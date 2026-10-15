"""树形 diff 的分级与路径格式测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from replay.differ import (
    MISSING,
    canonical_path,
    child_path,
    diff_all,
    diff_headers,
    diff_json,
    index_path,
    terminal_key,
)
from replay.model import (
    DiffLevel,
    IgnoreRules,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    ReplayOutcome,
)

pytestmark = pytest.mark.replay


def levels(diffs) -> list[DiffLevel]:
    return [d.level for d in diffs]


def by_level(diffs, level: DiffLevel):
    return [d for d in diffs if d.level is level]


# --------------------------------------------------------------------------
# 三档分级各命中一次
# --------------------------------------------------------------------------


def test_structure_type_value_are_graded_separately():
    expected = {
        "data": {
            "qty": 3,  # VALUE：值变了
            "name": "widget",  # TYPE：string -> int
            "items": [{"sku": "A"}, {"sku": "B"}],  # STRUCTURE：数组变长
        }
    }
    actual = {
        "data": {
            "qty": 4,
            "name": 7,
            "items": [{"sku": "A"}, {"sku": "B"}, {"sku": "C"}],
        }
    }

    diffs = diff_json(expected, actual, IgnoreRules())

    structure = by_level(diffs, DiffLevel.STRUCTURE)
    type_changes = by_level(diffs, DiffLevel.TYPE)
    values = by_level(diffs, DiffLevel.VALUE)

    assert [d.path for d in structure] == ["$.data.items[2]"]
    assert structure[0].expected == MISSING
    assert structure[0].actual == {"sku": "C"}

    assert [d.path for d in type_changes] == ["$.data.name"]
    assert type_changes[0].expected == "widget"
    assert type_changes[0].actual == 7

    assert [d.path for d in values] == ["$.data.qty"]
    assert values[0].expected == 3
    assert values[0].actual == 4


def test_key_added_and_removed_are_both_structure():
    diffs = diff_json({"a": 1, "b": 2}, {"a": 1, "c": 3}, IgnoreRules())
    assert {d.path: d.level for d in diffs} == {
        "$.b": DiffLevel.STRUCTURE,
        "$.c": DiffLevel.STRUCTURE,
    }
    removed = next(d for d in diffs if d.path == "$.b")
    assert removed.actual == MISSING
    assert removed.expected == 2


def test_int_float_are_same_type_and_graded_value():
    """1 与 1.0 属同一类型 number，差异应落在 VALUE 而不是 TYPE。"""
    diffs = diff_json({"x": 1}, {"x": 1.5}, IgnoreRules())
    assert [(d.path, d.level) for d in diffs] == [("$.x", DiffLevel.VALUE)]


def test_bool_is_not_number():
    diffs = diff_json({"flag": True}, {"flag": 1}, IgnoreRules())
    assert [(d.path, d.level) for d in diffs] == [("$.flag", DiffLevel.TYPE)]


def test_none_vs_missing_distinguishes_null_from_absence():
    """null 是合法值（TYPE 变化），键消失是结构变化。"""
    type_case = diff_json({"a": None}, {"a": 0}, IgnoreRules())
    assert [(d.path, d.level) for d in type_case] == [("$.a", DiffLevel.TYPE)]

    structure_case = diff_json({"a": None}, {}, IgnoreRules())
    assert [(d.path, d.level) for d in structure_case] == [("$.a", DiffLevel.STRUCTURE)]
    assert structure_case[0].actual == MISSING


def test_differ_never_emits_noise():
    """differ 不得产出 NOISE —— 那是 analyzer 的职责。"""
    expected = {"t": "2024-01-01T00:00:00Z", "uid": "0" * 8, "id": 1, "x": 1}
    actual = {"t": "2024-06-01T00:00:00Z", "uid": "1" * 8, "id": 2, "x": 2}
    diffs = diff_json(expected, actual, IgnoreRules())
    assert diffs, "应产出差异"
    assert all(d.level is not DiffLevel.NOISE for d in diffs)
    assert all(d.reason is None for d in diffs)


# --------------------------------------------------------------------------
# 路径格式
# --------------------------------------------------------------------------


def test_paths_are_jsonpath_style():
    expected = {"data": {"items": [{"qty": 1}]}}
    actual = {"data": {"items": [{"qty": 2}]}}
    diffs = diff_json(expected, actual, IgnoreRules())
    assert [d.path for d in diffs] == ["$.data.items[0].qty"]


def test_path_helpers():
    assert child_path("$", "data") == "$.data"
    assert child_path("$.data", "items") == "$.data.items"
    assert child_path("$.data", "x-trace") == '$.data["x-trace"]'
    assert index_path("$.data.items", 3) == "$.data.items[3]"
    assert canonical_path("$.data.items[3].qty") == "$.data.items[].qty"
    assert terminal_key("$.data.items[0].qty") == "qty"
    assert terminal_key("$.data.items[0]") == "items"


def test_deep_nesting_and_multi_value_arrays():
    expected = {"layers": [{"in": [{"v": 1}, {"v": 2}]}]}
    actual = {"layers": [{"in": [{"v": 1}, {"v": 9}]}]}
    diffs = diff_json(expected, actual, IgnoreRules())
    assert [(d.path, d.level) for d in diffs] == [
        ("$.layers[0].in[1].v", DiffLevel.VALUE)
    ]


# --------------------------------------------------------------------------
# 忽略规则
# --------------------------------------------------------------------------


def test_ignored_paths_drop_the_diff_entirely():
    rules = IgnoreRules(ignored_paths=("$.data.created_at",))
    diffs = diff_json(
        {"a": 1, "data": {"created_at": "2024-01-01T00:00:00Z", "n": 1}},
        {"a": 1, "data": {"created_at": "2025-01-01T00:00:00Z", "n": 2}},
        rules,
    )
    assert [d.path for d in diffs] == ["$.data.n"]


def test_ignored_path_prefixes_drop_a_whole_subtree():
    rules = IgnoreRules(ignored_path_prefixes=("$.data.trace",))
    diffs = diff_json(
        {"data": {"trace": {"id": "a", "span": "b"}, "n": 1}},
        {"data": {"trace": {"id": "c", "span": "d"}, "n": 2}},
        rules,
    )
    assert [d.path for d in diffs] == ["$.data.n"]


def test_ignored_path_hides_structure_changes_too():
    """整段不比意味着「这块怎么变都不产出差异」，包括键增删。"""
    rules = IgnoreRules(ignored_paths=("$.meta",))
    diffs = diff_json({"meta": {"a": 1}, "n": 1}, {"meta": {}, "n": 1}, rules)
    assert diffs == ()


def test_unordered_paths_compare_arrays_as_sets():
    rules = IgnoreRules(unordered_paths=("$.data.tags",))
    left = {"data": {"tags": ["a", "b", "c"]}}
    right = {"data": {"tags": ["c", "a", "b"]}}
    assert diff_json(left, right, rules) == ()

    right_missing = {"data": {"tags": ["c", "a"]}}
    diffs = diff_json(left, right_missing, rules)
    # 集合比：缺了一个元素属结构变化（排序对齐后落在尾部）
    assert DiffLevel.STRUCTURE in [d.level for d in diffs]
    assert any(d.actual == MISSING for d in diffs)


def test_unordered_only_applies_to_the_named_path():
    left = {"data": {"tags": ["a", "b"]}, "other": ["a", "b"]}
    right = {"data": {"tags": ["b", "a"]}, "other": ["b", "a"]}
    rules = IgnoreRules(unordered_paths=("$.data.tags",))
    diffs = diff_json(left, right, rules)
    assert [d.path for d in diffs] == ["$.other[0]", "$.other[1]"]


def test_unordered_matches_indexed_subpaths():
    rules = IgnoreRules(unordered_paths=("$.data.tags",))
    left = {"data": {"tags": [{"v": 1}, {"v": 2}]}}
    right = {"data": {"tags": [{"v": 2}, {"v": 1}]}}
    assert diff_json(left, right, rules) == ()


def test_float_tolerance_absorbs_small_float_drift():
    rules = IgnoreRules(float_tolerance=0.01)
    assert diff_json({"price": 10.0}, {"price": 10.005}, rules) == ()
    diffs = diff_json({"price": 10.0}, {"price": 10.5}, rules)
    assert [(d.path, d.level) for d in diffs] == [("$.price", DiffLevel.VALUE)]


def test_zero_tolerance_still_reports_float_drift():
    diffs = diff_json({"price": 10.0}, {"price": 10.0000001}, IgnoreRules())
    assert [(d.path, d.level) for d in diffs] == [("$.price", DiffLevel.VALUE)]


def test_ignored_headers_are_skipped():
    rules = IgnoreRules(ignored_headers=("date", "content-length", "server", "set-cookie"))
    expected = {
        "content-type": "application/json",
        "date": "Mon, 01 Jan 2024 00:00:00 GMT",
        "server": "nginx/1.0",
        "content-length": "12",
    }
    actual = {
        "content-type": "application/json",
        "date": "Tue, 02 Jan 2024 00:00:00 GMT",
        "server": "nginx/2.0",
        "content-length": "99",
    }
    assert diff_headers(expected, actual, rules) == ()


def test_header_names_are_case_insensitive():
    rules = IgnoreRules(ignored_headers=())
    assert diff_headers({"Content-Type": "a"}, {"content-type": "a"}, rules) == ()
    diffs = diff_headers({"Content-Type": "a"}, {"content-type": "b"}, rules)
    assert [(d.path, d.level) for d in diffs] == [
        ('$.headers["content-type"]', DiffLevel.VALUE)
    ]


def test_header_added_and_removed():
    rules = IgnoreRules(ignored_headers=())
    diffs = diff_headers({"a": "1", "b": "2"}, {"a": "1", "c": "3"}, rules)
    assert {d.path for d in diffs} == {"$.headers.b", "$.headers.c"}


# --------------------------------------------------------------------------
# exchange 级比对
# --------------------------------------------------------------------------


def _exchange(exchange_id: str, body_json, status: int = 200, headers=None):
    request = RecordedRequest(method="GET", url="http://x/api", path="/api")
    return RecordedExchange(
        exchange_id=exchange_id,
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        request=request,
        response=RecordedResponse(
            status=status,
            headers=headers or {"content-type": "application/json"},
            body=json.dumps(body_json).encode(),
            body_json=body_json,
        ),
    )


def _outcome(exchange_id: str, body_json, status: int = 200, headers=None, error=None):
    return ReplayOutcome(
        exchange_id=exchange_id,
        status=status,
        headers=headers or {"content-type": "application/json"},
        body=None if body_json is None else json.dumps(body_json).encode(),
        body_json=body_json,
        elapsed_ms=5.0,
        error=error,
    )


def test_status_change_is_reported():
    verdict = diff_all([_exchange("e1", {"a": 1}, status=200)], [_outcome("e1", {"a": 1}, status=500)])[0]
    assert verdict.status_changed is True
    assert ("$.status", DiffLevel.VALUE) in [(d.path, d.level) for d in verdict.diffs]


def test_non_json_body_compares_length_only():
    """纯文本/二进制：只比状态码与长度，不比内容。"""
    request = RecordedRequest(method="GET", url="http://x/f", path="/f")
    baseline = RecordedExchange(
        exchange_id="f1",
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        request=request,
        response=RecordedResponse(
            status=200,
            headers={"content-type": "application/octet-stream"},
            body=b"\x00\x01\x02\x03",
            body_json=None,
        ),
    )
    same_length = ReplayOutcome(
        exchange_id="f1",
        status=200,
        headers={"content-type": "application/octet-stream"},
        body=b"\xff\xfe\xfd\xfc",  # 内容完全不同、长度相同
        body_json=None,
    )
    assert diff_all([baseline], [same_length])[0].diffs == ()

    longer = ReplayOutcome(
        exchange_id="f1",
        status=200,
        headers={"content-type": "application/octet-stream"},
        body=b"\x00\x01\x02\x03\x04",
        body_json=None,
    )
    diffs = diff_all([baseline], [longer])[0].diffs
    assert [d.path for d in diffs] == ["$.body"]
    assert diffs[0].expected == 4
    assert diffs[0].actual == 5


def test_json_becomes_non_json_is_a_type_change():
    verdict = diff_all(
        [_exchange("e1", {"a": 1})],
        [_outcome("e1", None, headers={"content-type": "text/html"})],
    )[0]
    assert ("$.body", DiffLevel.TYPE) in [(d.path, d.level) for d in verdict.diffs]


def test_unmatched_exchange_is_marked_as_new_traffic():
    verdicts = diff_all([_exchange("e1", {"a": 1})], [_outcome("e2", {"a": 1})])
    assert verdicts[0].matched_baseline is False
    assert verdicts[0].diffs == ()
    assert verdicts[0].has_real_regression is False


def test_transport_failure_produces_error_verdict():
    verdict = diff_all([_exchange("e1", {"a": 1})], [_outcome("e1", None, status=None, error="ConnectError: refused")])[0]
    assert verdict.error == "ConnectError: refused"
    assert verdict.diffs == ()
    assert verdict.has_real_regression is False


def test_matched_flag_is_true_for_paired_exchange():
    verdict = diff_all([_exchange("e1", {"a": 1})], [_outcome("e1", {"a": 2})])[0]
    assert verdict.matched_baseline is True
    assert verdict.has_real_regression is True
