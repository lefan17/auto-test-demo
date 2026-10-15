"""度量库自身：建表幂等、落库、读回、flaky、漂移历史。

每个用例都用 ``tmp_path`` 下的独立 SQLite，绝不碰 ``qaplat/.cache/qaplat.db``：
真库是流水线的历史资产，被测试写脏之后「上一轮到底是不是红的」就再也无法回答。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from qaplat import metrics
from qaplat.metrics import (
    MetricsStore,
    RunRecord,
    build_run_record,
    case_outcomes_from_pairs,
    new_run_id,
    outcome_of,
    pass_rate_of,
    utc_now,
)

pytestmark = pytest.mark.platform


@pytest.fixture
def store(tmp_path) -> Iterator[MetricsStore]:
    opened = MetricsStore(tmp_path / "qaplat.db")
    opened.migrate()
    yield opened
    opened.close()


def _run(
    store: MetricsStore, *, layer: str, passed: int, failed: int = 0, errors: int = 0, seconds: float = 1.0
) -> str:
    run_id = new_run_id(layer)
    record = build_run_record(
        run_id=run_id,
        layer=layer,
        commit_sha="a49b097",
        started_at=utc_now(),
        duration_seconds=seconds,
        counts={"passed": passed, "failed": failed, "error": errors},
    )
    store.record_run(record)
    return run_id


# ------------------------------------------------------------------ schema


def test_migrate_is_idempotent(store: MetricsStore, tmp_path) -> None:
    before = store.schema_version
    store.migrate()
    store.migrate()
    assert store.schema_version == before == metrics.SCHEMA_VERSION


def test_schema_version_is_written_to_pragma(tmp_path) -> None:
    """版本必须写进 ``PRAGMA user_version`` 而不是某张表：空库也能读出「该用哪个 schema」。"""
    path = tmp_path / "v.db"
    MetricsStore(path).migrate()
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == metrics.SCHEMA_VERSION
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert {"runs", "case_outcomes", "contract_drift"} <= tables


def test_migrate_on_fresh_path_creates_parent_dir(tmp_path) -> None:
    nested = tmp_path / "a" / "b" / "qaplat.db"
    MetricsStore(nested).migrate()
    assert nested.exists()


def test_store_is_context_manager(tmp_path) -> None:
    with MetricsStore(tmp_path / "ctx.db") as opened:
        opened.migrate()
        assert opened.schema_version == metrics.SCHEMA_VERSION


# ------------------------------------------------------------------ 落库与读回


def test_roundtrip_preserves_every_counter(store: MetricsStore) -> None:
    run_id = new_run_id("nightly")
    store.record_run(
        RunRecord(
            run_id=run_id,
            layer="nightly",
            commit_sha="cafe123",
            started_at=utc_now(),
            duration_seconds=123.5,
            total=10,
            passed=7,
            failed=1,
            errors=1,
            skipped=1,
            xfailed=0,
            pass_rate=pass_rate_of(7, 1, 10),
            outcome=outcome_of(7, 1, 1, 10),
        )
    )
    back = store.recent_runs()[0]
    assert back.run_id == run_id
    assert (back.total, back.passed, back.failed, back.errors, back.skipped, back.xfailed) == (10, 7, 1, 1, 1, 0)
    assert back.duration_seconds == pytest.approx(123.5)
    assert back.commit_sha == "cafe123"
    assert back.outcome == "failed"


def test_started_at_survives_roundtrip_as_utc(store: MetricsStore) -> None:
    """时间戳必须带时区读回来：不带偏移的字符串会被静默当成本地时间。"""
    run_id = new_run_id("smoke")
    store.record_run(
        RunRecord(
            run_id=run_id,
            layer="smoke",
            commit_sha=None,
            started_at=utc_now(),
            duration_seconds=1.0,
            total=1,
            passed=1,
            failed=0,
            errors=0,
            skipped=0,
            xfailed=0,
            pass_rate=1.0,
            outcome="passed",
        )
    )
    back = store.recent_runs()[0]
    assert back.started_at.tzinfo is not None
    assert back.started_at.utcoffset() == pytest.approx(__import__("datetime").timedelta(0))


def test_recent_runs_newest_first_and_limited(store: MetricsStore) -> None:
    ids = [_run(store, layer="smoke", passed=1) for _ in range(5)]
    runs = store.recent_runs(limit=3)
    assert len(runs) == 3
    # 同一秒内建的多轮靠 rowid 兜底排序，最后写入的排最前。
    assert runs[0].run_id == ids[-1]


def test_recording_same_run_id_twice_replaces(store: MetricsStore) -> None:
    """重跑 gate 补数据时是覆盖写，不是主键冲突。"""
    run_id = _run(store, layer="smoke", passed=1)
    _run(store, layer="smoke", passed=2)  # 另一轮，制造干扰
    store.record_run(
        build_run_record(
            run_id=run_id,
            layer="smoke",
            commit_sha=None,
            started_at=utc_now(),
            duration_seconds=9.0,
            counts={"passed": 9},
        )
    )
    same = [r for r in store.recent_runs() if r.run_id == run_id]
    assert len(same) == 1
    assert same[0].passed == 9


def test_latest_run_can_be_filtered_by_layer(store: MetricsStore) -> None:
    _run(store, layer="smoke", passed=1)
    nightly_id = _run(store, layer="nightly", passed=2)
    assert store.latest_run("nightly") is not None
    assert store.latest_run("nightly").run_id == nightly_id  # type: ignore[union-attr]
    assert store.latest_run("release") is None


def test_case_outcomes_are_stored_and_read_per_run(store: MetricsStore) -> None:
    run_id = new_run_id("pr")
    store.record_run(
        build_run_record(
            run_id=run_id,
            layer="pr",
            commit_sha=None,
            started_at=utc_now(),
            duration_seconds=1.0,
            counts={"passed": 1, "failed": 1},
        ),
        case_outcomes_from_pairs(
            run_id,
            [("testcases/api/test_a.py::test_x", "passed", 0.1), ("testcases/api/test_b.py::test_y", "failed", 0.2)],
        ),
    )
    outcomes = store.run_case_outcomes(run_id)
    assert [c.case_id for c in outcomes] == ["testcases/api/test_a.py::test_x", "testcases/api/test_b.py::test_y"]
    assert [c.outcome for c in outcomes] == ["passed", "failed"]


def test_illegal_outcome_is_rejected_before_it_reaches_the_db() -> None:
    with pytest.raises(ValueError, match="非法用例结论"):
        case_outcomes_from_pairs("r1", [("c1", "flaky", 0.0)])


# ------------------------------------------------------------------ flaky


def _record_cases(store: MetricsStore, layer: str, rows: dict[str, str]) -> None:
    run_id = new_run_id(layer)
    store.record_run(
        build_run_record(
            run_id=run_id,
            layer=layer,
            commit_sha=None,
            started_at=utc_now(),
            duration_seconds=1.0,
            counts={
                "passed": sum(1 for v in rows.values() if v == "passed"),
                "failed": sum(1 for v in rows.values() if v == "failed"),
            },
        ),
        case_outcomes_from_pairs(run_id, [(case, outcome, 0.1) for case, outcome in rows.items()]),
    )


def test_flaky_lists_only_cases_that_both_passed_and_failed(store: MetricsStore) -> None:
    _record_cases(store, "smoke", {"c_flaky": "passed", "c_stable": "passed"})
    _record_cases(store, "smoke", {"c_flaky": "failed", "c_stable": "passed"})
    assert store.flaky_cases() == [("c_flaky", 1, 2)]


def test_flaky_counts_pass_rounds_out_of_appearance_rounds(store: MetricsStore) -> None:
    for outcome in ("failed", "passed", "failed", "passed", "passed"):
        _record_cases(store, "pr", {"c_shaky": outcome})
    assert store.flaky_cases() == [("c_shaky", 3, 5)]


def test_skipped_and_error_rounds_are_not_appearances(store: MetricsStore) -> None:
    """跳过一轮再通过一轮不是 flaky——用例压根没跑，不是不稳定。"""
    _record_cases(store, "nightly", {"c_skip": "skipped"})
    _record_cases(store, "nightly", {"c_skip": "passed"})
    _record_cases(store, "nightly", {"c_skip": "error"})
    assert store.flaky_cases() == []


def test_flaky_window_limits_which_rounds_count(store: MetricsStore) -> None:
    _record_cases(store, "smoke", {"c_old": "failed"})
    _record_cases(store, "smoke", {"c_old": "passed"})
    assert store.flaky_cases(last_n_runs=1) == []
    assert store.flaky_cases(last_n_runs=2) == [("c_old", 1, 2)]


def test_flaky_on_empty_db_is_empty_not_an_error(store: MetricsStore) -> None:
    assert store.flaky_cases() == []
    assert store.recent_runs() == []


# ------------------------------------------------------------------ 漂移与聚合


def test_drift_history_roundtrip(store: MetricsStore) -> None:
    store.record_drift("sha1", True, ["docs/openapi.json"])
    store.record_drift("sha2", False, [])
    history = store.contract_drift_history()
    assert history[0].commit_sha == "sha2"
    assert history[0].drifted is False
    assert history[1].changed == ("docs/openapi.json",)
    assert history[1].drifted is True
    assert history[1].checked_at.tzinfo is not None


def test_same_commit_drift_check_is_recorded_once(store: MetricsStore) -> None:
    """同一 commit 重复检查是覆盖写：漂移历史是按基线提交去重的检查记录，不是事件流。"""
    store.record_drift("sha1", True, ["a.json"])
    store.record_drift("sha1", False, [])
    assert len(store.contract_drift_history()) == 1
    assert store.contract_drift_history()[0].drifted is False


def test_layer_summary_aggregates_per_layer(store: MetricsStore) -> None:
    _run(store, layer="smoke", passed=4, seconds=10.0)
    _run(store, layer="smoke", passed=2, failed=2, seconds=20.0)
    _run(store, layer="pr", passed=1, seconds=30.0)
    summary = dict((row[0], row) for row in store.layer_summary())
    assert summary["smoke"][1] == 2
    assert summary["smoke"][2] == pytest.approx((1.0 + 0.5) / 2)
    assert summary["smoke"][3] == pytest.approx(15.0)
    assert summary["pr"][1] == 1


# ------------------------------------------------------------------ 通过率口径


@pytest.mark.parametrize(
    ("passed", "failed", "expected"),
    [(10, 0, 1.0), (0, 10, 0.0), (5, 5, 0.5), (0, 0, 0.0)],
)
def test_pass_rate_never_exceeds_one(passed: int, failed: int, expected: float) -> None:
    assert pass_rate_of(passed, failed, passed + failed) == pytest.approx(expected)


def test_skipped_does_not_dilute_pass_rate() -> None:
    """36 通过 + 4 跳过是 100% 通过，不是 90%：跳过不是失败。"""
    assert pass_rate_of(36, 0, 40) == pytest.approx(1.0)


def test_all_errors_is_zero_pass_rate() -> None:
    assert pass_rate_of(0, 0, 6) == 0.0
    assert outcome_of(0, 0, 6, 6) == "error"


def test_empty_run_is_error_not_passed() -> None:
    assert outcome_of(0, 0, 0, 0) == "error"
