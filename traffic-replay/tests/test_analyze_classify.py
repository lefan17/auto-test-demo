"""差异分类测试：易变值降级、reason 说明、四类判定。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from analyze.classify import (
    DEFAULT_NOISE_RULES,
    AutoIncrementIdRule,
    CategorySummary,
    DiffCategory,
    FloatEpsilonRule,
    NoisePolicy,
    TimestampRule,
    UuidRule,
    apply_noise_policy,
    categorize,
    classify_all,
    classify_verdict,
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


def verdict(diffs=(), *, error=None, matched=True, exchange_id="e1") -> ExchangeVerdict:
    return ExchangeVerdict(
        exchange_id=exchange_id,
        replayed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        matched_baseline=matched,
        diffs=tuple(diffs),
        error=error,
    )


def diff(path, expected, actual, level=DiffLevel.VALUE) -> FieldDiff:
    return FieldDiff(path=path, level=level, expected=expected, actual=actual)


# --------------------------------------------------------------------------
# 规则命中
# --------------------------------------------------------------------------


def test_iso_timestamp_is_noise_with_reason():
    d = diff("$.data.created_at", "2024-01-01T00:00:00Z", "2024-06-05T12:00:00Z")
    reason = NoisePolicy().explain(d)
    assert reason is not None
    assert reason.startswith("noise:timestamp")


def test_epoch_timestamp_is_noise():
    d = diff("$.ts", 1700000000, 1710000000)
    assert (NoisePolicy().explain(d) or "").startswith("noise:timestamp")


def test_time_like_field_name_with_plain_values_is_still_noise():
    """字段名带时间语义（*_at）时，取值不是时间串也算时间戳漂移。"""
    d = diff("$.data.synced_at", "v1", "v2")
    assert (NoisePolicy().explain(d) or "").startswith("noise:timestamp")


def test_uuid_is_noise():
    d = diff(
        "$.data.trace_id",
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "9c858901-8a57-4791-81fe-4c455b099bc9",
    )
    assert (NoisePolicy().explain(d) or "").startswith("noise:uuid")


def test_autoincrement_id_within_window_is_noise():
    d = diff("$.data.id", 1001, 1002)
    assert (NoisePolicy().explain(d) or "").startswith("noise:autoincrement_id")


def test_autoincrement_id_beyond_window_is_real():
    """ID 突然跳到一个完全不同的量级，不是「序列整体后移」，得人看。"""
    d = diff("$.data.id", 1001, 999999)
    assert NoisePolicy().explain(d) is None


def test_float_tail_drift_is_noise():
    d = diff("$.data.score", 0.1 + 0.2, 0.3)
    assert (NoisePolicy().explain(d) or "").startswith("noise:float_epsilon")


def test_meaningful_float_change_is_real():
    d = diff("$.data.score", 1.0, 2.0)
    assert NoisePolicy().explain(d) is None


def test_structure_and_type_are_never_downgraded():
    """键没了、类型变了，是契约在动，跟取值易变不是一回事。"""
    for level in (DiffLevel.STRUCTURE, DiffLevel.TYPE):
        d = diff("$.data.created_at", "2024-01-01T00:00:00Z", 1, level=level)
        assert NoisePolicy().explain(d) is None, f"{level} 不应被降级"


def test_every_rule_has_a_distinct_name():
    names = [r.name for r in DEFAULT_NOISE_RULES]
    assert len(names) == len(set(names)) == 4
    assert set(names) == {"timestamp", "uuid", "autoincrement_id", "float_epsilon"}


def test_rules_can_be_narrowed_via_ignore_rules_whitelist():
    """volatile_value_fingerprints 非空 = 显式白名单，只留列出的规则。"""
    policy = NoisePolicy.from_ignore_rules(
        IgnoreRules(volatile_value_fingerprints=("noise:uuid",))
    )
    assert [r.name for r in policy.rules] == ["uuid"]
    assert policy.explain(diff("$.created_at", "2024-01-01", "2025-01-01")) is None
    assert policy.explain(
        diff("$.uid", "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "9c858901-8a57-4791-81fe-4c455b099bc9")
    ) is not None


def test_rule_thresholds_are_configurable():
    loose = AutoIncrementIdRule(max_id_delta=100000)
    assert loose.hit(diff("$.id", 1, 50000)) is not None
    strict = FloatEpsilonRule(epsilon=0.0)
    assert strict.hit(diff("$.x", 1.0, 1.0 + 1e-12)) is None
    assert TimestampRule().hit(diff("$.at", "2024-01-01", "2024-01-02")) is not None


# --------------------------------------------------------------------------
# 降级后的 FieldDiff
# --------------------------------------------------------------------------


def test_degraded_diff_keeps_values_and_gains_reason():
    original = diff("$.created_at", "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")
    (downgraded,) = apply_noise_policy((original,), NoisePolicy())
    assert downgraded.level is DiffLevel.NOISE
    assert downgraded.reason is not None and downgraded.reason.startswith("noise:")
    # 原值必须保留，否则人无法核对「凭什么忽略」
    assert downgraded.path == original.path
    assert downgraded.expected == original.expected
    assert downgraded.actual == original.actual
    assert original.level is DiffLevel.VALUE  # 入参不可变


def test_classify_verdict_does_not_mutate_input():
    original = verdict([diff("$.id", 1, 2)])
    out = classify_verdict(original)
    assert original.diffs[0].level is DiffLevel.VALUE
    assert out.diffs[0].level is DiffLevel.NOISE


# --------------------------------------------------------------------------
# 四类判定
# --------------------------------------------------------------------------


def test_categorize_clean_when_no_diffs():
    assert categorize(verdict()) is DiffCategory.CLEAN


def test_categorize_noise_when_only_noise():
    categorized = classify_verdict(verdict([diff("$.id", 1, 2)]))
    assert categorize(categorized) is DiffCategory.NOISE


def test_categorize_regression_when_any_real_diff():
    categorized = classify_verdict(
        verdict([diff("$.id", 1, 2), diff("$.data.qty", 1, 9)])
    )
    assert categorize(categorized) is DiffCategory.REGRESSION


def test_categorize_new_contract_when_unmatched():
    assert categorize(verdict(matched=False)) is DiffCategory.NEW_CONTRACT


def test_categorize_env_failure_takes_priority():
    """请求没打通时一切字段结论都不可信，环境失败优先。"""
    v = verdict([diff("$.data.qty", 1, 9)], error="ConnectError: refused")
    assert categorize(v) is DiffCategory.ENV_FAILURE


def test_env_failure_wins_over_new_contract():
    assert categorize(verdict(matched=False, error="Timeout")) is DiffCategory.ENV_FAILURE


def test_four_categories_are_counted_separately():
    verdicts = [
        verdict(),  # clean
        classify_verdict(verdict([diff("$.id", 1, 2)], exchange_id="n1")),  # noise
        verdict([diff("$.data.qty", 1, 9)], exchange_id="r1"),  # regression
        verdict(matched=False, exchange_id="new1"),  # new_contract
        verdict(error="Timeout", exchange_id="bad1"),  # env_failure
    ]
    _, summary = classify_all(verdicts)
    assert summary.counts[DiffCategory.CLEAN.value] == 1
    assert summary.counts[DiffCategory.NOISE.value] == 1
    assert summary.counts[DiffCategory.REGRESSION.value] == 1
    assert summary.counts[DiffCategory.NEW_CONTRACT.value] == 1
    assert summary.counts[DiffCategory.ENV_FAILURE.value] == 1
    assert summary.regression_exchanges == ("r1",)


def test_noise_reason_counts_group_by_rule():
    verdicts = [
        classify_verdict(
            verdict(
                [
                    diff("$.a_at", "2024-01-01", "2024-01-02"),
                    diff("$.b_at", "2024-02-01", "2024-03-02"),
                    diff("$.id", 1, 2),
                ],
                exchange_id="x",
            )
        )
    ]
    _, summary = classify_all(verdicts)
    assert summary.noise_reason_counts == {"noise:timestamp": 2, "noise:autoincrement_id": 1}


def test_noise_counting_is_per_diff_not_per_exchange():
    verdicts = [
        classify_verdict(
            verdict(
                [diff(f"$.items[{i}].created_at", "2024-01-01", "2024-02-01") for i in range(5)]
            )
        )
    ]
    classified, summary = classify_all(verdicts)
    assert len(classified[0].diffs) == 5
    assert all(d.level is DiffLevel.NOISE for d in classified[0].diffs)
    assert len(summary.noise_diffs) == 5  # 条数口径
    assert len(summary.exchange_ids[DiffCategory.NOISE.value]) == 1  # 接口口径


# --------------------------------------------------------------------------
# 端到端：differ → classify 衔接
# --------------------------------------------------------------------------


def _pair(request_path, baseline_json, actual_json):
    request = RecordedRequest(method="GET", url="http://x" + request_path, path=request_path)
    ex = RecordedExchange(
        exchange_id="e-" + request_path,
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        request=request,
        response=RecordedResponse(
            status=200,
            headers={"content-type": "application/json"},
            body_json=baseline_json,
        ),
    )
    import json

    outcome = ReplayOutcome(
        exchange_id=ex.exchange_id,
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(actual_json).encode(),
        body_json=actual_json,
    )
    return ex, outcome


def test_differ_then_classify_end_to_end():
    """differ 只报结构/类型/值，classify 才把易变值降成噪声。"""
    baseline = {
        "id": 1001,
        "created_at": "2024-01-01T00:00:00Z",
        "uid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "qty": 3,
    }
    actual = {
        "id": 1002,
        "created_at": "2024-06-01T00:00:00Z",
        "uid": "9c858901-8a57-4791-81fe-4c455b099bc9",
        "qty": 4,
    }
    ex, outcome = _pair("/api/items", baseline, actual)

    raw = diff_all([ex], [outcome])[0]
    assert len(raw.diffs) == 4
    assert all(d.level is not DiffLevel.NOISE for d in raw.diffs)

    classified = classify_verdict(raw)
    kinds = {d.path: d.level for d in classified.diffs}
    assert kinds == {
        "$.id": DiffLevel.NOISE,
        "$.created_at": DiffLevel.NOISE,
        "$.uid": DiffLevel.NOISE,
        "$.qty": DiffLevel.VALUE,
    }
    assert categorize(classified) is DiffCategory.REGRESSION
