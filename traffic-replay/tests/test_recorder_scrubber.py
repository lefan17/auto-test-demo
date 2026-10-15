"""录制端测试：脱敏的稳定性与格式保真（不破坏可回放性）。"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from recorder.scrubber import DEFAULT_SEED, ScrubRule, Scrubber
from replay.model import (
    Dependency,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    normalize_headers,
)

pytestmark = pytest.mark.recorder

PHONE = "13800138000"
ID_CARD = "110101199003078515"
BANK_CARD = "6222021234567890"
EMAIL = "limin@example.com"
ADDRESS = "上海市浦东新区世纪大道100号"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMDAxIiwibmFtZSI6IkxpIE1pbmcifQ"
    ".dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)
TOKEN = "sk-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"


def _exchange(
    *,
    path: str = "/api/v1/users/1001",
    body_json=None,
    response_json=None,
    headers: dict[str, str] | None = None,
) -> RecordedExchange:
    body = None if body_json is None else json.dumps(body_json).encode()
    res = None if response_json is None else json.dumps(response_json).encode()
    request = RecordedRequest(
        method="POST",
        url=f"http://api.demo.local{path}",
        path=path,
        headers=normalize_headers(headers or {"Content-Type": "application/json"}),
        query={},
        body=body,
        body_json=body_json,
    )
    response = RecordedResponse(status=200, headers={"content-type": "application/json"}, body=res, body_json=response_json, elapsed_ms=12.0)
    return RecordedExchange(
        exchange_id="fixed-id-0001",
        recorded_at=datetime(2025, 1, 6, 2, 15, 3, tzinfo=timezone.utc),
        request=request,
        response=response,
        tags={"env": "staging"},
    )


# -- 稳定性：核心难点 -------------------------------------------------------


def test_same_phone_maps_to_same_fake_across_exchanges() -> None:
    """同一手机号出现在两个不同 RecordedExchange 里，必须映射到同一个假值。"""
    scrubber = Scrubber()
    first = scrubber.scrub_exchange(
        _exchange(body_json={"phone": PHONE}, response_json={"data": {"contact": f"tel:{PHONE}"}})
    )
    second = scrubber.scrub_exchange(
        _exchange(
            path="/api/v2/orders",
            response_json={"items": [{"receiver_phone": PHONE}]},
        )
    )

    fake_in_first = first.request.body_json["phone"]
    fake_in_first_response = first.response.body_json["data"]["contact"].removeprefix("tel:")
    fake_in_second = second.response.body_json["items"][0]["receiver_phone"]

    print(
        f"\n[scrub-stability] exchange#1.request.body_json.phone: {PHONE} -> {fake_in_first}"
        f"\n[scrub-stability] exchange#1.response.body_json.data.contact: tel:{PHONE} -> tel:{fake_in_first_response}"
        f"\n[scrub-stability] exchange#2.response.body_json.items[0].receiver_phone: {PHONE} -> {fake_in_second}"
    )
    assert fake_in_first == fake_in_first_response == fake_in_second
    assert fake_in_first != PHONE


def test_same_value_maps_to_same_fake_across_scrubber_instances() -> None:
    """跨实例（≈跨进程）稳定：映射只由 seed + 原值决定，不吃 Python 的 hash 随机化。"""
    a = Scrubber(seed="seed-A")
    b = Scrubber(seed="seed-A")
    other = Scrubber(seed="seed-B")

    assert a.scrub_text(PHONE) == b.scrub_text(PHONE)
    assert a.scrub_text(PHONE) != other.scrub_text(PHONE), "不同 seed 应给出不同假值"


def test_two_identical_recordings_produce_identical_bytes() -> None:
    """同一份流量脱敏两次，字节完全一致 —— 否则基线配对会全错。"""
    payload_request = {"phone": PHONE, "id_card": ID_CARD, "email": EMAIL, "note": ADDRESS}
    payload_response = {"ts": "2025-01-06T02:15:03Z", "seq": 1002, "phone": PHONE}

    def run() -> tuple[bytes | None, bytes | None]:
        scrubber = Scrubber()
        out = scrubber.scrub_exchange(
            _exchange(body_json=payload_request, response_json=payload_response)
        )
        return out.request.body, out.response.body

    assert run() == run()


# -- 格式保真 ---------------------------------------------------------------


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def test_phone_keeps_11_digits_and_valid_prefix() -> None:
    fake = Scrubber().scrub_text(PHONE)
    assert re.fullmatch(r"1[3-9]\d{9}", fake), fake
    assert len(fake) == len(PHONE)


def test_id_card_keeps_18_chars_and_checksum() -> None:
    fake = Scrubber().scrub_text(ID_CARD)
    assert re.fullmatch(r"[1-9]\d{16}[\dXx]", fake), fake
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    expected = "10X98765432"[sum(int(c) * w for c, w in zip(fake[:17], weights)) % 11]
    assert fake[17] in (expected, expected.lower())


def test_bank_card_keeps_length_and_luhn() -> None:
    fake = Scrubber().scrub_text(BANK_CARD)
    assert fake.isdigit()
    assert len(fake) == len(BANK_CARD)
    assert _luhn_ok(fake), fake
    assert fake != BANK_CARD


def test_email_keeps_shape_and_domain() -> None:
    fake = Scrubber().scrub_text(EMAIL)
    assert re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", fake), fake
    assert fake.endswith("@" + EMAIL.partition("@")[2])
    assert len(fake.partition("@")[0]) == len(EMAIL.partition("@")[0])


def test_jwt_keeps_three_segments_and_header_alg() -> None:
    fake = Scrubber().scrub_text(JWT)
    segments = fake.split(".")
    assert len(segments) == 3
    assert len(segments[1]) == len(JWT.split(".")[1])
    header = json.loads(base64.urlsafe_b64decode(segments[0] + "=" * (-len(segments[0]) % 4)))
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert fake != JWT


def test_bearer_jwt_keeps_prefix() -> None:
    fake = Scrubber().scrub_text(f"Bearer {JWT}")
    assert fake.startswith("Bearer ")
    assert fake[len("Bearer ") :] != JWT


def test_token_keeps_prefix_and_length() -> None:
    fake = Scrubber().scrub_text(TOKEN)
    assert fake.startswith("sk-")
    assert len(fake) == len(TOKEN)
    assert fake != TOKEN


def test_address_keeps_length_and_chinese() -> None:
    fake = Scrubber().scrub_text(ADDRESS)
    assert len(fake) == len(ADDRESS)
    assert all("\u4e00" <= ch <= "\u9fa5" for ch in fake), fake
    assert fake != ADDRESS


def test_separators_in_phone_are_preserved() -> None:
    fake = Scrubber().scrub_text("138-0013-8000")
    assert re.fullmatch(r"1[3-9]\d-\d{4}-\d{4}", fake), fake


# -- 可回放性：脱敏不得破坏结构 ---------------------------------------------


def test_scrubbing_preserves_replay_critical_fields() -> None:
    original = _exchange(
        body_json={"phone": PHONE},
        response_json={"phone": PHONE, "seq": 42, "ts": "2025-01-06T02:15:03Z"},
    )
    scrubbed = Scrubber().scrub_exchange(original)

    assert scrubbed.exchange_id == original.exchange_id
    assert scrubbed.recorded_at == original.recorded_at
    assert scrubbed.request.method == original.request.method
    assert scrubbed.request.path == original.request.path
    assert scrubbed.response.status == original.response.status
    assert scrubbed.response.elapsed_ms == original.response.elapsed_ms
    assert scrubbed.tags == original.tags
    # 非敏感字段原样保留
    assert scrubbed.response.body_json["seq"] == 42
    assert scrubbed.response.body_json["ts"] == "2025-01-06T02:15:03Z"


def test_original_exchange_is_not_mutated() -> None:
    original = _exchange(body_json={"phone": PHONE})
    Scrubber().scrub_exchange(original)
    assert original.request.body_json == {"phone": PHONE}
    assert json.loads(original.request.body)["phone"] == PHONE


def test_query_and_url_are_scrubbed() -> None:
    request = RecordedRequest(
        method="GET",
        url=f"http://api.demo.local/api/v1/users?keyword={PHONE}",
        path="/api/v1/users",
        query={"keyword": [PHONE]},
    )
    exchange = RecordedExchange(
        exchange_id="q1",
        recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
        request=request,
        response=RecordedResponse(status=200),
    )
    out = Scrubber().scrub_exchange(exchange)
    assert out.request.query["keyword"][0] != PHONE
    assert PHONE not in out.request.url
    assert out.request.query["keyword"][0] in out.request.url


# -- 依赖产出 ---------------------------------------------------------------


def test_dependencies_record_original_to_scrubbed_mapping() -> None:
    scrubber = Scrubber()
    scrubbed = scrubber.scrub_with_deps(
        _exchange(body_json={"phone": PHONE, "email": EMAIL}, response_json={"id_card": ID_CARD})
    )
    deps = {d.kind: d for d in scrubber.dependencies}
    print(f"\n[依赖产出] {scrubber.dependencies}")

    assert set(deps) >= {"phone", "email", "id_card"}
    assert deps["phone"].ref == PHONE
    assert deps["phone"].scrubbed != PHONE
    assert deps["id_card"].scrubbed != ID_CARD
    assert scrubbed.deps and all(isinstance(d, Dependency) for d in scrubbed.deps)
    # 依赖表里的假值必须与请求体里实际写入的假值一致，否则造数取不到
    assert deps["phone"].scrubbed == scrubbed.request.body_json["phone"]


def test_dependencies_are_deduplicated_by_kind_and_value() -> None:
    scrubber = Scrubber()
    scrubber.scrub_text(PHONE)
    scrubber.scrub_text(PHONE)
    scrubber.scrub_text(PHONE)
    assert len(scrubber.dependencies) == 1
    scrubber.reset_dependencies()
    assert scrubber.dependencies == ()


def test_repeated_value_is_still_attached_to_the_later_exchange() -> None:
    """同一手机号在第 2、3 条交换里再次出现时，仍必须挂到那两条交换上。

    否则「依赖表里查不到归属」——回放前造数会漏掉后面的请求。
    """
    scrubber = Scrubber()
    first = scrubber.scrub_with_deps(_exchange(body_json={"phone": PHONE}))
    second = scrubber.scrub_with_deps(
        _exchange(path="/api/v2/orders", body_json={"receiver": PHONE})
    )
    third = scrubber.scrub_with_deps(
        _exchange(path="/api/v3/notes", response_json={"notify": PHONE})
    )

    assert [d.kind for d in first.deps] == ["phone"]
    assert [d.ref for d in second.deps] == [PHONE], "重复值必须仍归属到这一条交换"
    assert [d.ref for d in third.deps] == [PHONE]
    assert second.deps == third.deps == first.deps


def test_scrub_with_deps_keeps_preexisting_deps() -> None:
    original = _exchange(body_json={"phone": PHONE})
    carried = Dependency(kind="order", ref="SO-1", scrubbed="SO-9")
    original = replace(original, deps=(carried,))
    out = Scrubber().scrub_with_deps(original)
    assert carried in out.deps
    assert {d.kind for d in out.deps} == {"order", "phone"}


def test_scrubbed_and_original_dependency_share_same_kind() -> None:
    """宽松手机号规则与严格规则的 kind 相同，合并成同一种依赖（kind=phone）。"""
    scrubber = Scrubber()
    assert scrubber.scrub_text(PHONE) != PHONE
    assert {d.kind for d in scrubber.dependencies} == {"phone"}


# -- 可配置扩展 -------------------------------------------------------------


def test_extra_rules_extend_coverage() -> None:
    rule = ScrubRule("employee_no", r"EMP\d{6}", kind="employee")
    scrubber = Scrubber(extra_rules=[rule])
    fake = scrubber.scrub_text("EMP123456")
    assert fake != "EMP123456"
    assert len(fake) == len("EMP123456")
    assert scrubber.dependencies[0].kind == "employee"
    assert scrubber.dependencies[0].ref == "EMP123456"


def test_extra_rules_do_not_disturb_builtins() -> None:
    scrubber = Scrubber(extra_rules=[ScrubRule("employee_no", r"EMP\d{6}")])
    assert scrubber.scrub_text(PHONE) == Scrubber().scrub_text(PHONE)


def test_default_seed_is_stable_constant() -> None:
    assert DEFAULT_SEED == "traffic-replay/scrubber/v1"
    assert Scrubber().scrub_text(PHONE) == Scrubber(seed=DEFAULT_SEED).scrub_text(PHONE)
