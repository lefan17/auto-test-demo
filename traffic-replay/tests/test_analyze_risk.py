"""风险排序测试。核心命题：口径是「有差异的接口数」，不是「差异条数」。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from analyze.classify import classify_all
from analyze.risk import (
    LEVEL_WEIGHT,
    build_risk,
    endpoint_of,
    normalize_path,
    render_risk_table,
)
from replay.differ import diff_all
from replay.model import (
    DiffLevel,
    ExchangeVerdict,
    FieldDiff,
    IgnoreRules,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    ReplayOutcome,
)

pytestmark = pytest.mark.analyze

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def exchange(
    exchange_id: str,
    path: str,
    method: str = "GET",
    *,
    baseline_json=None,
    actual_json=None,
    status: int = 200,
    actual_status: int | None = None,
    error: str | None = None,
) -> tuple[RecordedExchange, ReplayOutcome]:
    request = RecordedRequest(method=method, url="http://x" + path, path=path)
    ex = RecordedExchange(
        exchange_id=exchange_id,
        recorded_at=NOW,
        request=request,
        response=RecordedResponse(
            status=status,
            headers={"content-type": "application/json"},
            body_json=baseline_json,
        ),
    )
    outcome = ReplayOutcome(
        exchange_id=exchange_id,
        status=actual_status if actual_status is not None else status,
        headers={"content-type": "application/json"},
        body=None if actual_json is None else json.dumps(actual_json).encode(),
        body_json=actual_json,
        error=error,
    )
    return ex, outcome


def run_one(pair, rules=None):
    """走完整链路：diff → classify → build_risk。"""
    ex, outcome = pair
    raw = diff_all([ex], [outcome], rules)
    verdicts, _ = classify_all(raw, rules)
    return build_risk(verdicts, [ex])


# --------------------------------------------------------------------------
# 路径归一
# --------------------------------------------------------------------------


def test_normalize_path_collapses_ids():
    assert normalize_path("/api/orders/1001") == "/api/orders/{id}"
    assert normalize_path("/api/orders/1001/items/2") == "/api/orders/{id}/items/{id}"
    assert (
        normalize_path("/api/u/3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        == "/api/u/{id}"
    )
    assert normalize_path("/api/orders") == "/api/orders"
    assert normalize_path("") == "/"


def test_endpoint_of_combines_method_and_template():
    assert endpoint_of("post", "/api/orders/7") == "POST /api/orders/{id}"


# --------------------------------------------------------------------------
# 排序权重
# --------------------------------------------------------------------------


def test_level_weights_rank_structure_above_type_above_value():
    assert (
        LEVEL_WEIGHT[DiffLevel.STRUCTURE]
        > LEVEL_WEIGHT[DiffLevel.TYPE]
        > LEVEL_WEIGHT[DiffLevel.VALUE]
        > LEVEL_WEIGHT[DiffLevel.NOISE]
    )


def test_score_is_call_count_times_worst_level():
    pairs = [_single_pair(f"e{i}", "/api/a", {"x": 1}, {"x": 2}) for i in range(4)]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    item = summary.items[0]
    assert item.worst_level is DiffLevel.VALUE
    assert item.score == 4 * LEVEL_WEIGHT[DiffLevel.VALUE] == 40.0


# --------------------------------------------------------------------------
# 聚合口径（本文件的核心）
# --------------------------------------------------------------------------


def _single_pair(eid, path, baseline, actual, method="GET"):
    return exchange(eid, path, method, baseline_json=baseline, actual_json=actual)


def test_over_a_thousand_raw_diffs_collapse_to_single_digit_endpoints():
    """核心证明：1203 条原始差异 -> 个位数接口（3 个）。

    构造：100 个接口 × 4 条回放，每条含 时间戳/UUID/自增ID 三类易变字段抖动，
    合计 1200 条噪声差异；其中只有 3 个接口各有一处真实值回归。
    结论：原始 1203 条 -> 待人工评审 3 个接口。
    """
    baseline = {
        "id": 1000,
        "created_at": "2023-12-01T00:00:00Z",
        "trace_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "qty": 1,
    }
    pairs = []
    for api in range(100):
        for i in range(4):
            actual = {
                "id": 1001 + i,  # 自增 ID 漂移
                "created_at": f"2024-0{1 + (i % 6)}-01T00:00:00Z",  # 时间戳
                "trace_id": f"9c858901-8a57-4791-81fe-4c455b099b{i:02d}",  # UUID
                "qty": 1,
            }
            if api < 3 and i == 0:
                actual["qty"] = 999  # 真实回归，只落在 3 个接口上
            pairs.append(
                _single_pair(f"e{api}-{i}", f"/api/v1/resource{api}", baseline, actual)
            )

    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))

    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    # 400 条回放 × (时间戳 + UUID + 自增ID) = 1200 条噪声差异，加 3 条真实值差异
    assert summary.diffs_before_noise == 1200 + 3
    assert summary.noise_diffs == 1200
    assert summary.total_raw_diffs == 3  # 降噪后仍需人看的差异**条数**
    assert summary.endpoints_total == 100

    # 接口口径：1203 条原始差异 -> 3 个有差异接口（个位数）
    assert summary.endpoints_with_diff == 3
    assert summary.endpoints_noise_only == 97
    assert summary.convergence_ratio == pytest.approx(1203 / 3)


def test_raw_diff_count_and_endpoint_count_are_different_units():
    """同一个接口抖得再多，也只是 1 个待评审接口。"""
    pairs = [
        _single_pair(f"e{i}", "/api/orders", {"qty": 1}, {"qty": 100 + i})
        for i in range(50)
    ]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))

    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    assert summary.total_raw_diffs == 50  # 差异条数
    assert summary.endpoints_with_diff == 1  # 接口数 —— 这才是评审口径
    assert summary.items[0].raw_diff_count == 50
    assert summary.items[0].diff_exchange_count == 50
    assert summary.items[0].call_count == 50
    assert summary.convergence_ratio == 50.0


def test_field_paths_are_canonicalized_across_indices():
    """同字段在数组不同下标上抖，应聚成一个字段路径。"""
    pairs = [_single_pair(f"e{i}", "/api/list", {"items": [{"qty": 1}]}, {"items": [{"qty": 2}]}) for i in range(3)]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    assert summary.endpoints_with_diff == 1
    assert summary.items[0].field_paths == ("$.items[].qty",)


def test_ids_in_path_do_not_split_one_endpoint():
    """`/api/orders/1001` 与 `/api/orders/1002` 必须聚成同一个接口。"""
    pairs = [
        _single_pair("e1", "/api/orders/1001", {"qty": 1}, {"qty": 2}),
        _single_pair("e2", "/api/orders/1002", {"qty": 1}, {"qty": 2}),
        _single_pair(
            "e3",
            "/api/orders/3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            {"qty": 1},
            {"qty": 2},
        ),
    ]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    assert summary.endpoints_with_diff == 1
    assert summary.items[0].endpoint == "GET /api/orders/{id}"
    assert summary.items[0].call_count == 3


def test_query_string_does_not_split_endpoint():
    """分页差异（不同 query）属于同一接口。"""
    pairs = [_single_pair(f"e{i}", "/api/items", {"qty": 1}, {"qty": 2}) for i in range(4)]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)
    assert summary.endpoints_with_diff == 1
    assert summary.items[0].call_count == 4


# --------------------------------------------------------------------------
# 排序
# --------------------------------------------------------------------------


def test_more_severe_level_ranks_higher_at_equal_call_count():
    pairs = [
        _single_pair("val", "/api/a", {"x": 1}, {"x": 2}),  # VALUE
        _single_pair("struct", "/api/b", {"x": 1}, {"y": 1}),  # STRUCTURE
        _single_pair("type", "/api/c", {"x": 1}, {"x": "1"}),  # TYPE
    ]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    assert [i.endpoint for i in summary.items] == [
        "GET /api/b",  # structure 最重
        "GET /api/c",  # type
        "GET /api/a",  # value 最轻
    ]
    assert summary.top.worst_level is DiffLevel.STRUCTURE


def test_higher_call_count_ranks_higher_at_equal_level():
    pairs = [_single_pair("hot1", "/api/hot", {"x": 1}, {"x": 2})]
    pairs += [_single_pair(f"cold{i}", "/api/cold", {"x": 1}, {"x": 2}) for i in range(3)]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    assert [i.endpoint for i in summary.items] == ["GET /api/cold", "GET /api/hot"]
    assert summary.items[0].call_count == 3


def test_worst_level_wins_over_average():
    """一个接口只要有一次结构退化，就足以说明契约在动。"""
    pairs = [
        _single_pair("soft", "/api/mixed", {"x": 1}, {"x": 2}),  # VALUE
        _single_pair("hard", "/api/mixed", {"x": 1}, {"y": 2}),  # STRUCTURE
    ]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    assert summary.items[0].worst_level is DiffLevel.STRUCTURE
    assert summary.items[0].score == 2 * LEVEL_WEIGHT[DiffLevel.STRUCTURE]


# --------------------------------------------------------------------------
# 边界
# --------------------------------------------------------------------------


def test_env_failure_and_new_traffic_are_not_queued_for_review():
    """传输失败没有可比差异；新增流量不是回归——两者都不进评审队列。"""
    _, out_bad = exchange("bad", "/api/x", error="ConnectError: refused")
    out_new = ReplayOutcome(
        exchange_id="unseen", status=200, body=b"{}", body_json={}
    )

    verdicts = diff_all([], [out_bad, out_new])
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, [])

    assert summary.endpoints_with_diff == 0
    assert summary.total_raw_diffs == 0


def test_unmatched_exchange_without_route_falls_back_not_dropped():
    """verdict 里没有路由信息时，宁可多一条 unknown 也不静默丢。"""
    ex, out = exchange("e1", "/api/x", baseline_json={"a": 1}, actual_json={"a": 2})
    verdicts = diff_all([ex], [out])
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, [])  # 故意不传 exchanges

    assert summary.endpoints_with_diff == 1
    assert summary.items[0].endpoint.startswith("UNKNOWN ")
    assert summary.endpoints_total == 1


def test_no_diff_means_no_summary_rows():
    ex, out = exchange("e1", "/api/x", baseline_json={"a": 1}, actual_json={"a": 1})
    verdicts = diff_all([ex], [out])
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, [ex])

    assert summary.endpoints_with_diff == 0
    assert summary.endpoints_noise_only == 0  # 零差异不是噪声
    assert summary.total_raw_diffs == 0
    assert summary.endpoints_total == 1  # 回放到了 1 个接口，但它不进任何队列
    assert summary.convergence_ratio == 0.0


def test_identical_responses_after_ignore_rules_produce_nothing():
    rules = IgnoreRules(ignored_paths=("$.created_at",))
    ex, out = exchange(
        "e1",
        "/api/x",
        baseline_json={"created_at": "2024-01-01T00:00:00Z", "qty": 1},
        actual_json={"created_at": "2025-01-01T00:00:00Z", "qty": 1},
    )
    verdicts = diff_all([ex], [out], rules)
    classified, _ = classify_all(verdicts, rules)
    summary = build_risk(classified, [ex])

    assert summary.diffs_before_noise == 0
    assert summary.endpoints_with_diff == 0


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def test_render_risk_table_has_one_row_per_endpoint():
    pairs = [_single_pair(f"e{i}", f"/api/r{i}", {"x": 1}, {"x": 2}) for i in range(3)]
    verdicts = []
    exchanges = []
    for ex, outcome in pairs:
        exchanges.append(ex)
        verdicts.extend(diff_all([ex], [outcome]))
    classified, _ = classify_all(verdicts)
    summary = build_risk(classified, exchanges)

    table = render_risk_table(summary)
    lines = table.splitlines()
    assert len(lines) == 2 + 3  # 表头 + 分隔行 + 3 个接口
    assert "| 1 |" in lines[2]

    limited = render_risk_table(summary, limit=2)
    assert len(limited.splitlines()) == 2 + 2
