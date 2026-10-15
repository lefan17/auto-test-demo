"""录制端测试：SQLite 落库的 round-trip、幂等迁移与报告读写。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from recorder.store import SCHEMA_VERSION, BaselineInfo, Repository
from replay.model import (
    Dependency,
    DiffLevel,
    ExchangeVerdict,
    FieldDiff,
    ReplayReport,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
)

pytestmark = pytest.mark.recorder

UTC = timezone.utc


def _exchange(n: int) -> RecordedExchange:
    body_json: dict[str, Any] = {"name": "李敏", "phone": "13800138000", "nested": {"n": n}}
    return RecordedExchange(
        exchange_id=f"id-{n:04d}",
        recorded_at=datetime(2025, 1, 6, 2, 15, 3, 123456, tzinfo=UTC) + timedelta(seconds=n),
        request=RecordedRequest(
            method="POST",
            url=f"http://api.demo.local/api/v1/users?page={n}&tag=a&tag=b",
            path="/api/v1/users",
            headers={"content-type": "application/json", "x-trace-id": f"t{n}"},
            query={"page": [str(n)], "tag": ["a", "b"]},
            body=json.dumps(body_json, ensure_ascii=False).encode("utf-8"),
            body_json=body_json,
        ),
        response=RecordedResponse(
            status=200 + n,
            headers={"content-type": "application/json"},
            body=b'{"code":0}',
            body_json={"code": 0, "seq": n},
            elapsed_ms=12.5 + n,
        ),
        tags={"env": "staging", "biz": "user"},
        deps=(Dependency(kind="phone", ref="13800138000", scrubbed="14130290158"),),
    )


def _empty_bodies_exchange() -> RecordedExchange:
    """body / body_json 全空的最坏情况。"""
    return RecordedExchange(
        exchange_id="id-empty",
        recorded_at=datetime(2025, 1, 6, tzinfo=UTC),
        request=RecordedRequest(method="GET", url="http://api.demo.local/health", path="/health"),
        response=RecordedResponse(status=204),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Any:
    r = Repository(tmp_path / "traffic.sqlite3")
    yield r
    r.close()


# -- round-trip：任务书要求的头号证据 ---------------------------------------


def test_round_trip_preserves_equality(repo: Repository) -> None:
    original = [_exchange(1), _exchange(2), _empty_bodies_exchange()]
    baseline_id = repo.save_baseline("smoke", original)
    loaded = repo.load_baseline(baseline_id)

    print(f"\n[round-trip] 写入 {len(original)} 条，读回 {len(loaded)} 条")
    for before, after in zip(original, loaded):
        print(f"[round-trip] {before.exchange_id}: equal={before == after}")

    assert loaded == original, "load_baseline 必须还原到与原对象相等的 RecordedExchange"
    assert loaded[0] is not original[0]


def test_round_trip_field_by_field(repo: Repository) -> None:
    original = _exchange(7)
    loaded = repo.load_baseline(repo.save_baseline("fields", [original]))[0]

    assert loaded.exchange_id == original.exchange_id
    assert loaded.recorded_at == original.recorded_at
    assert loaded.recorded_at.tzinfo is not None
    assert loaded.request == original.request
    assert loaded.response == original.response
    assert loaded.tags == original.tags
    assert loaded.deps == original.deps
    assert loaded.request.body == original.request.body
    assert loaded.request.body_json == original.request.body_json
    assert loaded.request.query == original.request.query
    assert loaded.response.elapsed_ms == original.response.elapsed_ms


def test_round_trip_survives_reopening_the_file(tmp_path: Path) -> None:
    db = tmp_path / "reopen.sqlite3"
    original = [_exchange(1), _exchange(2)]
    with Repository(db) as r:
        baseline_id = r.save_baseline("persisted", original)
    with Repository(db) as r2:
        assert r2.load_baseline(baseline_id) == original
        assert r2.list_baselines()[0].exchange_count == 2


def test_round_trip_of_non_utf8_body(repo: Repository) -> None:
    """二进制体（如 protobuf / gzip）不能被当字符串二次编码。"""
    blob = b"\x00\x01\xff\xfe\x80gzip"
    exchange = RecordedExchange(
        exchange_id="id-binary",
        recorded_at=datetime(2025, 1, 6, tzinfo=UTC),
        request=RecordedRequest(
            method="POST", url="http://api.demo.local/upload", path="/upload", body=blob
        ),
        response=RecordedResponse(status=201, body=blob),
    )
    loaded = repo.load_baseline(repo.save_baseline("binary", [exchange]))[0]
    assert loaded.request.body == blob
    assert loaded.response.body == blob
    assert loaded == exchange


def test_round_trip_of_non_utc_timezone(tmp_path: Path) -> None:
    """recorded_at 可能带 +08:00 偏移，存 ISO 8601 后必须还原成同一个瞬间。"""
    tz8 = timezone(timedelta(hours=8))
    exchange = RecordedExchange(
        exchange_id="id-tz8",
        recorded_at=datetime(2025, 1, 6, 10, 15, 3, 500000, tzinfo=tz8),
        request=RecordedRequest(method="GET", url="http://api.demo.local/x", path="/x"),
        response=RecordedResponse(status=200),
    )
    with Repository(tmp_path / "tz.sqlite3") as repo:
        loaded = repo.load_baseline(repo.save_baseline("tz", [exchange]))[0]
    assert loaded.recorded_at == exchange.recorded_at
    assert loaded.recorded_at.utcoffset() == timedelta(hours=8)
    assert loaded == exchange


def test_round_trip_of_unicode_and_surrogate_free_text(repo: Repository) -> None:
    exchange = RecordedExchange(
        exchange_id="id-unicode",
        recorded_at=datetime(2025, 1, 6, tzinfo=UTC),
        request=RecordedRequest(
            method="POST",
            url="http://api.demo.local/备注",
            path="/备注",
            headers={"x-note": "中文 header 值"},
            body_json={"备注": "换行\n制表\t引号\"单引号'"},
        ),
        response=RecordedResponse(status=200, body_json={"emoji": "🙂"}),
    )
    loaded = repo.load_baseline(repo.save_baseline("unicode", [exchange]))[0]
    assert loaded == exchange
    assert loaded.request.body_json["备注"].count("\n") == 1


# -- 顺序与覆盖 -------------------------------------------------------------


def test_load_baseline_returns_write_order(repo: Repository) -> None:
    original = [_exchange(n) for n in (3, 1, 4, 1, 5)]
    loaded = repo.load_baseline(repo.save_baseline("ordered", original))
    assert [e.exchange_id for e in loaded] == [e.exchange_id for e in original]


def test_same_name_overwrites_previous_baseline(repo: Repository) -> None:
    first_id = repo.save_baseline("nightly", [_exchange(1)])
    second_id = repo.save_baseline("nightly", [_exchange(2), _exchange(3)])

    assert first_id != second_id
    assert repo.get_baseline_id("nightly") == second_id
    assert repo.list_baselines() == [
        BaselineInfo(
            name="nightly",
            baseline_id=second_id,
            created_at=repo.list_baselines()[0].created_at,
            exchange_count=2,
        )
    ]
    with pytest.raises(KeyError):
        repo.load_baseline(first_id)


def test_unknown_baseline_id_raises_key_error(repo: Repository) -> None:
    with pytest.raises(KeyError):
        repo.load_baseline("nope")


def test_get_baseline_id_returns_none_when_absent(repo: Repository) -> None:
    assert repo.get_baseline_id("absent") is None


def test_list_baselines_sorted_by_created_at_desc(repo: Repository) -> None:
    for name in ("a", "b", "c"):
        repo.save_baseline(name, [_exchange(1)])
    created = [info.created_at for info in repo.list_baselines()]
    assert created == sorted(created, reverse=True)
    assert len(repo.list_baselines()) == 3


def test_empty_baseline_is_listed_with_zero_count(repo: Repository) -> None:
    baseline_id = repo.save_baseline("empty", [])
    info = repo.list_baselines()[0]
    assert info.baseline_id == baseline_id
    assert info.exchange_count == 0
    assert repo.load_baseline(baseline_id) == []


def test_baseline_id_is_16_hex_chars(repo: Repository) -> None:
    baseline_id = repo.save_baseline("shape", [_exchange(1)])
    assert len(baseline_id) == 16
    int(baseline_id, 16)


def test_iter_baseline_matches_load_baseline(repo: Repository) -> None:
    original = [_exchange(1), _exchange(2)]
    baseline_id = repo.save_baseline("iter", original)
    assert list(repo.iter_baseline(baseline_id)) == original


# -- 迁移 -------------------------------------------------------------------


def test_migrate_is_idempotent(repo: Repository) -> None:
    repo.save_baseline("keep", [_exchange(1)])
    repo.migrate()
    repo.migrate()
    assert repo.schema_version == SCHEMA_VERSION
    assert len(repo.list_baselines()) == 1, "重复 migrate 不得清数据"


def test_schema_version_recorded_in_pragma_user_version(repo: Repository) -> None:
    conn = sqlite3.connect(str(repo.db_path))
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        conn.close()
    assert repo.schema_version == SCHEMA_VERSION


def test_constructor_creates_missing_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "traffic.sqlite3"
    with Repository(nested) as r:
        assert r.get_baseline_id("x") is None
    assert nested.exists()


def test_no_new_dependencies_beyond_stdlib(repo: Repository) -> None:
    tables = {
        row[0]
        for row in sqlite3.connect(str(repo.db_path))
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert {"baselines", "exchanges", "reports"} <= tables


# -- 报告 -------------------------------------------------------------------


def _report(baseline_id: str, *, n: int = 1) -> ReplayReport:
    start = datetime(2025, 1, 6, 3, 0, 0, tzinfo=UTC)
    verdicts = tuple(
        ExchangeVerdict(
            exchange_id=f"id-{i:04d}",
            replayed_at=start + timedelta(milliseconds=i),
            elapsed_ms=10.0 + i,
            matched_baseline=i % 2 == 0,
            status_changed=i == 1,
            diffs=(
                FieldDiff(
                    path=f"$.data.items[{i}].qty",
                    level=DiffLevel.VALUE,
                    expected=1,
                    actual=2,
                    reason=None,
                ),
            )
            if i % 2
            else (),
            error=None if i % 3 else "timeout",
        )
        for i in range(n)
    )
    return ReplayReport(
        baseline_id=baseline_id,
        started_at=start,
        finished_at=start + timedelta(seconds=5),
        verdicts=verdicts,
    )


def test_latest_report_returns_none_when_empty(repo: Repository) -> None:
    assert repo.latest_report() is None
    assert repo.latest_report("anything") is None


def test_report_round_trip(repo: Repository) -> None:
    baseline_id = repo.save_baseline("rep", [_exchange(1)])
    report = _report(baseline_id, n=4)
    repo.save_report(report)
    loaded = repo.latest_report(baseline_id)
    assert loaded is not None
    assert loaded.baseline_id == report.baseline_id
    assert loaded.started_at == report.started_at
    assert loaded.finished_at == report.finished_at
    assert loaded.verdicts == report.verdicts
    assert loaded.total == report.total
    assert [v.exchange_id for v in loaded.failed] == [v.exchange_id for v in report.failed]
    assert [v.exchange_id for v in loaded.regressed] == [v.exchange_id for v in report.regressed]
    assert [v.exchange_id for v in loaded.healthy] == [v.exchange_id for v in report.healthy]


def test_latest_report_is_scoped_by_baseline_and_picks_newest(repo: Repository) -> None:
    a = repo.save_baseline("A", [_exchange(1)])
    b = repo.save_baseline("B", [_exchange(2)])

    repo.save_report(_report(a, n=1))
    repo.save_report(_report(a, n=3))
    repo.save_report(_report(b, n=2))

    assert repo.latest_report(a).total == 3
    assert repo.latest_report(b).total == 2
    assert repo.latest_report() is not None


def test_save_report_does_not_require_existing_baseline(repo: Repository) -> None:
    """报告可以先于基线落库（例如基线来自外部文件）。"""
    repo.save_report(_report("external-baseline", n=1))
    assert repo.latest_report("external-baseline").total == 1
