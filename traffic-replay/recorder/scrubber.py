"""脱敏：把真实敏感值换成**稳定**假值，且不破坏可回放性。

两条硬要求：

1. **同原值 → 同假值**（HMAC 决定的映射，跨进程、跨 `RecordedExchange` 都一致）。
   否则同一个用户在两次录制里变成两个不同账号，回放时基线全部错配。
2. **保留格式与长度**：手机号脱敏后仍是 11 位数字（且 `1[3-9]` 开头），
   身份证仍 18 位（校验位自洽），银行卡仍过 Luhn，邮箱仍像邮箱。

同时产出 `Dependency` 列表（原值 `ref` + 脱敏值 `scrubbed`），
供回放前按原值造数 —— 这是「脱敏后仍能命中真实业务数据」的唯一线索。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from replay.model import (
    Dependency,
    JsonValue,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
)

__all__ = [
    "DEFAULT_SEED",
    "ScrubRule",
    "Scrubber",
    "default_scrubber",
]

DEFAULT_SEED = "traffic-replay/scrubber/v1"

_HEX = "0123456789abcdef"
_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
_B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_ADDR_POOL = "北京市上海市广州市深圳市朝阳区海淀区建国路中山路人民路建国门大街100号院"


def _hmac_hex(key: str, value: str, length: int) -> str:
    """稳定假值的唯一来源：同一 key+value 永远得到同一串十六进制。"""
    digest = hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest()
    out = digest.hex()
    while len(out) < length:
        digest = hmac.new(digest, value.encode("utf-8"), hashlib.sha256).digest()
        out += digest.hex()
    return out[:length]


def _fake_digits(key: str, value: str, length: int) -> str:
    return "".join(_HEX[int(c, 16) % 10] for c in _hmac_hex(key, value, length))


def _fake_from(key: str, value: str, length: int, alphabet: str) -> str:
    return "".join(alphabet[int(c, 16) % len(alphabet)] for c in _hmac_hex(key, value, length))


def _separator_mask(text: str) -> tuple[tuple[int, str], ...]:
    """记录分隔符（空格/连字符）在原串里的位置，便于原样回贴。"""
    return tuple((i, ch) for i, ch in enumerate(text) if not ch.isdigit())


def _reapply_separators(mask: tuple[tuple[int, str], ...], digits: str) -> str:
    if not mask:
        return digits
    chars = list(digits)
    for pos, ch in mask:
        if pos <= len(chars):
            chars.insert(pos, ch)
    return "".join(chars)


# --------------------------------------------------------------------------
# 规则
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScrubRule:
    """一条脱敏规则。

    `name` 是总正则里的命名组名（必须唯一）；`kind` 是写进 `Dependency.kind` 的类别名
    （默认同 `name`，这样同一类别的多个变体正则可以合并成同一种依赖）。
    `pattern` 按注册顺序（=优先级）拼进一次 `sub`，因此替换出的假值**不会**被后续规则再匹配。
    """

    name: str
    pattern: str
    rewriter: Any = None  # (match, seed) -> str；None 表示用默认重写器
    kind: str | None = None


class Scrubber:
    """确定性脱敏器：同 seed + 同原值 → 同假值。"""

    def __init__(
        self,
        *,
        seed: str = DEFAULT_SEED,
        extra_rules: Iterable[ScrubRule] = (),
    ) -> None:
        self.seed = seed
        self._rules: list[ScrubRule] = [*_BUILTIN_RULES, *extra_rules]
        self._deps: dict[tuple[str, str], Dependency] = {}
        self._current: list[Dependency] | None = None
        self._pattern = self._compile(self._rules)

    @staticmethod
    def _compile(rules: Iterable[ScrubRule]) -> re.Pattern[str]:
        parts = [f"(?P<{r.name}>{r.pattern})" for r in rules]
        return re.compile("|".join(parts))

    # -- 文本层 ------------------------------------------------------------

    def scrub_text(self, text: str) -> str:
        if not text:
            return text
        rules = {r.name: r for r in self._rules}
        seed = self.seed

        def _sub(m: re.Match[str]) -> str:
            name = m.lastgroup
            assert name is not None
            original = m.group(0)
            rule = rules[name]
            fake = rule.rewriter(m, seed) if rule.rewriter is not None else _default_rewrite(name, original, seed)
            kind = rule.kind or name
            dep = self._deps.setdefault((kind, original), Dependency(kind=kind, ref=original, scrubbed=fake))
            if self._current is not None:
                self._current.append(dep)
            return fake

        return self._pattern.sub(_sub, text)

    # -- 结构层 ------------------------------------------------------------

    def scrub_value(self, value: JsonValue) -> JsonValue:
        """递归脱敏 JSON 值。"""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, list):
            return [self.scrub_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub_value(v) for k, v in value.items()}
        return value

    def scrub_bytes(self, body: bytes | None) -> bytes | None:
        if body is None:
            return None
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        return self.scrub_text(text).encode("utf-8")

    def scrub_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        return {k: self.scrub_text(v) for k, v in headers.items()}

    def scrub_exchange(self, exchange: RecordedExchange) -> RecordedExchange:
        """返回脱敏后的新对象；累积的依赖用 `dependencies` 取。"""
        req = exchange.request
        res = exchange.response

        query = {k: [self.scrub_text(v) for v in vs] for k, vs in req.query.items()}

        body_json = self.scrub_value(req.body_json)
        body = req.body
        if body_json is not None:
            body = json.dumps(body_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            body = self.scrub_bytes(body)

        res_body_json = self.scrub_value(res.body_json)
        res_body = res.body
        if res_body_json is not None:
            res_body = json.dumps(res_body_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            res_body = self.scrub_bytes(res_body)

        new_request = replace(
            req,
            url=self.scrub_text(req.url),
            query=query,
            headers=self.scrub_headers(req.headers),
            body=body,
            body_json=body_json,
        )
        new_response = replace(
            res,
            headers=self.scrub_headers(res.headers),
            body=res_body,
            body_json=res_body_json,
        )
        return replace(exchange, request=new_request, response=new_response)

    # -- 依赖 --------------------------------------------------------------

    @property
    def dependencies(self) -> tuple[Dependency, ...]:
        """按首次出现顺序返回依赖，可直接写进 `RecordedExchange.deps`。"""
        return tuple(self._deps.values())

    def reset_dependencies(self) -> None:
        self._deps.clear()

    def scrub_with_deps(self, exchange: RecordedExchange) -> RecordedExchange:
        """一次到位：脱敏并把**本交换实际改写到的**依赖挂到 `deps` 上。

        与 `scrub_exchange` + `dependencies[-n:]` 的区别：同一个值在后面的交换里
        再次出现时，仍然会挂到那一条交换上 —— 按首次出现切片会让「第 2 次出现的手机号」
        在依赖表里查不到归属。
        """
        seen: list[Dependency] = []
        self._current = seen
        try:
            scrubbed = self.scrub_exchange(exchange)
        finally:
            self._current = None

        merged: dict[tuple[str, str], Dependency] = {(d.kind, d.ref): d for d in exchange.deps}
        for dep in seen:
            merged.setdefault((dep.kind, dep.ref), dep)
        return replace(scrubbed, deps=tuple(merged.values()))


def default_scrubber(**kwargs: Any) -> Scrubber:
    return Scrubber(**kwargs)


# --------------------------------------------------------------------------
# 内置重写器
# --------------------------------------------------------------------------


def _rewrite_phone(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    mask = _separator_mask(original)
    digits = _fake_digits(f"{seed}/phone", original, 11)
    if digits[0] != "1":
        digits = "1" + digits[1:]
    second = str(int(digits[1], 16) % 7 + 3)
    return _reapply_separators(mask, digits[0] + second + digits[2:])


def _rewrite_id_card(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    body = _fake_digits(f"{seed}/id", original, 17)
    if body[0] == "0":
        body = "1" + body[1:]
    return body + _id_tail(body)


_ID_CHECK_CODES = "10X98765432"


def _id_tail(body17: str) -> str:
    """GB 11643 校验位：加权和 mod 11 → "10X98765432"。"""
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    total = sum(int(c) * w for c, w in zip(body17, weights))
    return _ID_CHECK_CODES[total % 11]


def _rewrite_bank_card(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    mask = _separator_mask(original)
    digits = _fake_digits(f"{seed}/bank", original, len(original) - len(mask))
    return _reapply_separators(mask, _luhn_finish(digits))


def _luhn_finish(digits: str) -> str:
    """把末位改成使整串通过 Luhn 校验的值。"""
    body = digits[:-1] if len(digits) > 1 else "0"
    for d in "0123456789":
        candidate = body + d
        total = 0
        for i, ch in enumerate(reversed(candidate)):
            n = int(ch)
            if i % 2 == 1:
                n *= 2
                if n > 9:
                    n -= 9
            total += n
        if total % 10 == 0:
            return candidate
    return digits


def _rewrite_email(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    local, _, domain = original.partition("@")
    if not local:
        return original
    fake_local = "u" + _fake_from(f"{seed}/email", original, max(len(local) - 1, 1), _ALNUM)
    return f"{fake_local[: len(local)]}@{domain}"


def _rewrite_jwt(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    prefix = ""
    if original.lower().startswith("bearer"):
        idx = original.index("eyJ") if "eyJ" in original else len("Bearer")
        prefix, original = m.group(0)[:idx], m.group(0)[idx:].lstrip()
    segs = original.split(".")
    alg = _jwt_alg(segs[0]) or "HS256"
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT"}, separators=(",", ":")))
    payload_len = len(segs[1]) if len(segs) > 1 else 24
    payload = _fake_from(f"{seed}/jwt", m.group(0), payload_len, _B64URL)
    signature = _fake_from(f"{seed}/jwt/sig", m.group(0), len(segs[2]) if len(segs) > 2 else 43, _B64URL)
    return f"{prefix}{header}.{payload}.{signature}"


def _jwt_alg(seg: str) -> str | None:
    try:
        pad = "=" * (-len(seg) % 4)
        data = json.loads(_b64decode(seg + pad))
    except Exception:  # noqa: BLE001 - 解不开就当未知
        return None
    return data.get("alg") if isinstance(data, dict) else None


def _b64decode(seg: str) -> str:
    import base64

    return base64.urlsafe_b64decode(seg.encode()).decode("utf-8", errors="ignore")


def _b64url(raw: str) -> str:
    import base64

    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _rewrite_token(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    prefix = ""
    value = original
    for candidate in ("Bearer ", "bearer "):
        if original.startswith(candidate):
            prefix, value = original[: len(candidate)], original[len(candidate) :]
            break
    if value.startswith("sk-"):
        fake = "sk-" + _fake_from(f"{seed}/token", original, len(value) - 3, _ALNUM)
    elif all(c in _HEX for c in value.lower()):
        fake = "".join(_HEX[int(c, 16) % 16] for c in _hmac_hex(f"{seed}/token", original, len(value)))
    else:
        fake = _fake_from(f"{seed}/token", original, len(value), _ALNUM)
    return prefix + fake


def _rewrite_address(m: re.Match[str], seed: str) -> str:
    original = m.group(0)
    h = _hmac_hex(f"{seed}/address", original, len(original))
    return "".join(_ADDR_POOL[int(c, 16) % len(_ADDR_POOL)] for c in h)


def _default_rewrite(name: str, original: str, seed: str) -> str:
    """扩展规则的默认行为：保持长度与字符类别，稳定映射。"""
    alphabet = _ALNUM
    return _fake_from(f"{seed}/{name}", original, len(original), alphabet)


# 顺序 = 优先级：结构性强的先匹配，避免被宽泛规则吞掉。
_BUILTIN_RULES: tuple[ScrubRule, ...] = (
    ScrubRule(
        "jwt",
        r"(?:Bearer\s+)?eyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}",
        _rewrite_jwt,
    ),
    ScrubRule("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", _rewrite_email),
    ScrubRule(
        "id_card",
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dXx])",
        _rewrite_id_card,
    ),
    ScrubRule(
        "bank_card",
        r"(?<![\d-])\d{4}(?:[ -]?\d{4}){3,4}(?![\d-])",
        _rewrite_bank_card,
    ),
    ScrubRule(
        "phone",
        r"(?<!\d)1[3-9]\d(?:[ -]?\d{4}){2}(?![\d-])",
        _rewrite_phone,
    ),
    ScrubRule("token", r"(?:Bearer )?[A-Za-z0-9_-]{24,}", _rewrite_token),
    ScrubRule("phone_loose", r"(?<!\d)\d{11}(?!\d)", _rewrite_phone, kind="phone"),
    ScrubRule(
        "address",
        r"[\u4e00-\u9fa5]{2,10}(?:省|市|自治区)?[\u4e00-\u9fa5]{0,8}(?:区|县|镇|乡)"
        r"[\u4e00-\u9fa5\d]{1,20}(?:路|街|道)[\u4e00-\u9fa5\d]{1,10}(?:号|栋|楼|室|单元)",
        _rewrite_address,
    ),
)
