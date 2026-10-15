"""差异分类：把「机器眼里的不一样」收敛成「人该不该看」。

differ 只回答「哪里不一样」，它刻意不产出 `NOISE`；判断一条差异是否值得人看，
是业务口径，只应由这里赋予。降级为 `NOISE` 的四类易变值：

==================  =========================================================
规则名               命中条件
==================  =========================================================
`timestamp`         值形如 ISO 8601 / RFC 1123 时间，或字段名带时间语义
                    （`*_at` / `*_time` / `timestamp` / `*_ts` / `date`），
                    或两侧都是 >= 1e9 的 epoch 数。
`uuid`              两侧都是标准 UUID 文本（8-4-4-4-12 hex）。
`autoincrement_id`  字段名是 `id` / `*_id` / `*_no` / `*_seq`，两侧都是整数，
                    且差值在 `max_id_delta` 之内（默认 100）——自增序列整体后移
                    一位不该被当成回归。
`float_epsilon`     两侧都是数值，相对误差 <= `epsilon`（默认 1e-9）——
                    序列化/精度截断导致的浮点尾差。
==================  =========================================================

命中任一条即把该 `FieldDiff` 降级为 `NOISE`，并在 `reason` 里写清命中哪条规则
（`"noise:timestamp"` 这种格式），便于人反问「凭什么忽略这条」。

**噪声不改变结构/类型的严重性**：`STRUCTURE`/`TYPE` 永不降级 ——
键没了、类型变了，是接口契约在动，跟取值易变不是一回事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum

from replay.model import (
    DiffLevel,
    ExchangeVerdict,
    FieldDiff,
    IgnoreRules,
)
from replay.differ import canonical_path, terminal_key

#: ISO 8601 / RFC 1123 时间形态
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)
_RFC_RE = re.compile(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} ")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TIME_LEAF_RE = re.compile(
    r"(^|_)(at|time|timestamp|ts|date|datetime|expires)$|(^|_)(at|time|ts|date)$"
)
_ID_LEAF_RE = re.compile(r"(^|_)(id|no|seq|index)$")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# 噪声规则
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NoiseRule:
    """一条可解释的降噪规则：命中则返回 reason，未命中返回 None。"""

    name: str
    description: str

    def reason_for(self, diff: FieldDiff) -> str | None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def hit(self, diff: FieldDiff) -> str | None:
        if diff.level in (DiffLevel.STRUCTURE, DiffLevel.TYPE):
            # 结构/类型变化永不降级。
            return None
        reason = self.reason_for(diff)
        return f"noise:{self.name}({reason})" if reason else None


@dataclass(frozen=True)
class TimestampRule(NoiseRule):
    name: str = "timestamp"
    description: str = "时间戳易变"

    def reason_for(self, diff: FieldDiff) -> str | None:
        if isinstance(diff.expected, str) and isinstance(diff.actual, str):
            if _ISO_RE.match(diff.expected) or _RFC_RE.match(diff.expected):
                return "iso8601"
        if _is_number(diff.expected) and _is_number(diff.actual):
            if float(diff.expected) >= 1e9 and float(diff.actual) >= 1e9:
                return "epoch"
        if _TIME_LEAF_RE.search(terminal_key(diff.path)):
            return f"path:{terminal_key(diff.path)}"
        return None


@dataclass(frozen=True)
class UuidRule(NoiseRule):
    name: str = "uuid"
    description: str = "UUID 每次生成都不同"

    def reason_for(self, diff: FieldDiff) -> str | None:
        left, right = diff.expected, diff.actual
        if isinstance(left, str) and isinstance(right, str):
            if _UUID_RE.match(left) or _UUID_RE.match(right):
                return "uuid4-text"
        return None


@dataclass(frozen=True)
class AutoIncrementIdRule(NoiseRule):
    """自增主键整体后移：两侧都是整数、字段名是 id 系、差值在容忍窗口内。"""

    name: str = "autoincrement_id"
    description: str = "自增 ID 漂移"
    max_id_delta: int = 100

    def reason_for(self, diff: FieldDiff) -> str | None:
        if not _ID_LEAF_RE.search(terminal_key(diff.path)):
            return None
        if not (_is_number(diff.expected) and _is_number(diff.actual)):
            return None
        if not (float(diff.expected).is_integer() and float(diff.actual).is_integer()):
            return None
        delta = abs(float(diff.expected) - float(diff.actual))
        if delta <= self.max_id_delta:
            return f"delta<={self.max_id_delta}"
        return None


@dataclass(frozen=True)
class FloatEpsilonRule(NoiseRule):
    """浮点尾差：相对误差小于阈值，属序列化精度而非业务变化。"""

    name: str = "float_epsilon"
    description: str = "浮点尾差"
    epsilon: float = 1e-9

    def reason_for(self, diff: FieldDiff) -> str | None:
        if not (_is_number(diff.expected) and _is_number(diff.actual)):
            return None
        left, right = float(diff.expected), float(diff.actual)
        scale = max(1.0, abs(left), abs(right))
        if abs(left - right) <= self.epsilon * scale:
            return f"rel<={self.epsilon}"
        return None


DEFAULT_NOISE_RULES: tuple[NoiseRule, ...] = (
    TimestampRule(),
    UuidRule(),
    AutoIncrementIdRule(),
    FloatEpsilonRule(),
)


@dataclass(frozen=True)
class NoisePolicy:
    """降噪策略 = 启用的规则集合。

    `IgnoreRules.volatile_value_fingerprints` 非空时被视为**显式白名单**：
    只有列在里面的规则生效（空 = 全部默认规则生效）。
    """

    rules: tuple[NoiseRule, ...] = DEFAULT_NOISE_RULES

    @classmethod
    def from_ignore_rules(cls, rules: IgnoreRules | None = None) -> "NoisePolicy":
        names = rules.volatile_value_fingerprints if rules else ()
        if not names:
            return cls()
        wanted = {n.split(":", 1)[-1] for n in names}
        selected = tuple(r for r in DEFAULT_NOISE_RULES if r.name in wanted)
        return cls(rules=selected or DEFAULT_NOISE_RULES)

    def explain(self, diff: FieldDiff) -> str | None:
        """返回第一个命中的规则理由，未命中返回 None。"""
        for rule in self.rules:
            reason = rule.hit(diff)
            if reason:
                return reason
        return None


# --------------------------------------------------------------------------
# 差异分类
# --------------------------------------------------------------------------


class DiffCategory(StrEnum):
    """四类差异（外加 `CLEAN`：无差异，不算差异分类）。"""

    REGRESSION = "regression"  # 真实回归
    NEW_CONTRACT = "new_contract"  # 契约内新增
    NOISE = "noise"  # 噪声
    ENV_FAILURE = "env_failure"  # 环境失败
    CLEAN = "clean"  # 与基线一致


def apply_noise_policy(
    diffs: tuple[FieldDiff, ...], policy: NoisePolicy
) -> tuple[FieldDiff, ...]:
    """把命中的差异降级为 NOISE 并写 reason；未命中的原样保留。"""
    out: list[FieldDiff] = []
    for diff in diffs:
        reason = policy.explain(diff)
        if reason is None:
            out.append(diff)
        else:
            out.append(replace(diff, level=DiffLevel.NOISE, reason=reason))
    return tuple(out)


def classify_verdict(
    verdict: ExchangeVerdict,
    rules: IgnoreRules | None = None,
    policy: NoisePolicy | None = None,
) -> ExchangeVerdict:
    """对一条 verdict 施加降噪，返回新的 verdict（原对象不可变，不修改入参）。"""
    active = policy or NoisePolicy.from_ignore_rules(rules)
    return replace(verdict, diffs=apply_noise_policy(verdict.diffs, active))


def categorize(verdict: ExchangeVerdict) -> DiffCategory:
    """判定一条 verdict 属于四类里的哪一类（无差异返回 CLEAN）。

    优先级：环境失败 > 契约内新增 > 真实回归 > 噪声。
    环境失败优先是因为「请求根本没打通」时一切字段结论都不可信。
    """
    if verdict.error is not None:
        return DiffCategory.ENV_FAILURE
    if not verdict.matched_baseline:
        return DiffCategory.NEW_CONTRACT
    if not verdict.diffs:
        return DiffCategory.CLEAN
    if any(d.level is not DiffLevel.NOISE for d in verdict.diffs):
        return DiffCategory.REGRESSION
    return DiffCategory.NOISE


@dataclass(frozen=True)
class CategorySummary:
    """一条基线跑完后的分类账。"""

    counts: dict[str, int] = field(default_factory=dict)
    #: 每个分类下的 exchange_id
    exchange_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 被降级为噪声的字段差异（原始条数，非接口数）
    noise_diffs: tuple[FieldDiff, ...] = ()
    #: 真实回归涉及的全部字段差异
    regression_diffs: tuple[FieldDiff, ...] = ()

    @property
    def regression_exchanges(self) -> tuple[str, ...]:
        return self.exchange_ids.get(DiffCategory.REGRESSION.value, ())

    @property
    def noise_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for diff in self.noise_diffs:
            key = (diff.reason or "unknown").split("(", 1)[0]
            counts[key] = counts.get(key, 0) + 1
        return counts


def classify_all(
    verdicts: tuple[ExchangeVerdict, ...] | list[ExchangeVerdict],
    rules: IgnoreRules | None = None,
    policy: NoisePolicy | None = None,
) -> tuple[tuple[ExchangeVerdict, ...], CategorySummary]:
    """批量降噪 + 汇总，返回 (降噪后的 verdicts, 分类账)。"""
    active = policy or NoisePolicy.from_ignore_rules(rules)
    classified = tuple(classify_verdict(v, policy=active) for v in verdicts)

    counts: dict[str, int] = {c.value: 0 for c in DiffCategory}
    ids: dict[str, list[str]] = {c.value: [] for c in DiffCategory}
    noise: list[FieldDiff] = []
    regression: list[FieldDiff] = []
    for verdict in classified:
        category = categorize(verdict)
        counts[category.value] += 1
        ids[category.value].append(verdict.exchange_id)
        if category is DiffCategory.REGRESSION:
            regression.extend(d for d in verdict.diffs if d.level is not DiffLevel.NOISE)
        elif category is DiffCategory.NOISE:
            noise.extend(verdict.diffs)

    summary = CategorySummary(
        counts=counts,
        exchange_ids={k: tuple(v) for k, v in ids.items()},
        noise_diffs=tuple(noise),
        regression_diffs=tuple(regression),
    )
    return classified, summary


__all__ = [
    "AutoIncrementIdRule",
    "CategorySummary",
    "DEFAULT_NOISE_RULES",
    "DiffCategory",
    "FloatEpsilonRule",
    "NoisePolicy",
    "NoiseRule",
    "TimestampRule",
    "UuidRule",
    "apply_noise_policy",
    "canonical_path",
    "categorize",
    "classify_all",
    "classify_verdict",
]
