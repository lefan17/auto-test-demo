"""SQLite 落库：基线（录制结果）与回放报告的持久化。

严格按 `FROZEN_INTERFACES.md` §2 的签名实现；纯 `sqlite3`，无新依赖。

两条容易翻车的点，这里都按契约钉死：

1. `load_baseline` 必须能还原到与原对象**相等**的 `RecordedExchange`：
   `body` 存 BLOB、`body_json` 存 JSON 文本、`recorded_at` 存 ISO 8601 带时区，
   三者的反序列化口径与 `replay.model` 的构造口径一一对应。
2. `migrate()` 幂等：schema 版本写进 `PRAGMA user_version`，重复调用不报错、不清数据。

JSON 列（headers / query / tags）按 `sort_keys=True` 落盘：字典的插入序不属于数据语义
（`make_exchange_id` 也只按 sorted 键算 ID），键序归一化后「同一批流量两次入库」的
行字节完全一致，可以直接对 sqlite 文件做 diff/哈希来发现录制抖动。
query 的**值列表**顺序保留（多值参数顺序可能影响服务端行为），只对键排序。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

__all__ = ["SCHEMA_VERSION", "BaselineInfo", "Repository"]

SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS baselines (
        baseline_id TEXT PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exchanges (
        baseline_id   TEXT    NOT NULL REFERENCES baselines(baseline_id) ON DELETE CASCADE,
        position      INTEGER NOT NULL,
        exchange_id   TEXT    NOT NULL,
        recorded_at   TEXT    NOT NULL,
        req_method    TEXT    NOT NULL,
        req_url       TEXT    NOT NULL,
        req_path      TEXT    NOT NULL,
        req_headers   TEXT    NOT NULL,
        req_query     TEXT    NOT NULL,
        req_body      BLOB,
        req_body_json TEXT,
        res_status    INTEGER NOT NULL,
        res_headers   TEXT    NOT NULL,
        res_body      BLOB,
        res_body_json TEXT,
        res_elapsed_ms REAL,
        tags          TEXT    NOT NULL,
        deps          TEXT    NOT NULL,
        PRIMARY KEY (baseline_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reports (
        report_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        baseline_id TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        payload     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reports_baseline ON reports(baseline_id, report_id DESC)",
)


@dataclass(frozen=True)
class BaselineInfo:
    name: str
    baseline_id: str
    created_at: datetime  # tz-aware UTC
    exchange_count: int


class Repository:
    """单文件 SQLite 仓库。线程内共享同一连接（`check_same_thread=False`）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent and not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    # -- 生命周期 ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def migrate(self) -> None:
        """建表并写 schema 版本；幂等，可重复调用。"""
        with self._conn:
            for statement in _SCHEMA:
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- 基线 --------------------------------------------------------------

    def save_baseline(self, name: str, exchanges: Iterable[RecordedExchange]) -> str:
        """批量写入，返回 baseline_id；同名覆盖旧基线（含其全部交换）。"""
        rows = list(exchanges)
        baseline_id = uuid.uuid4().hex[:16]
        created_at = datetime.now(timezone.utc).isoformat()
        with self._conn:
            old = self._conn.execute(
                "SELECT baseline_id FROM baselines WHERE name = ?", (name,)
            ).fetchone()
            if old is not None:
                self._conn.execute("DELETE FROM exchanges WHERE baseline_id = ?", (old["baseline_id"],))
                self._conn.execute("DELETE FROM baselines WHERE baseline_id = ?", (old["baseline_id"],))
            self._conn.execute(
                "INSERT INTO baselines (baseline_id, name, created_at) VALUES (?, ?, ?)",
                (baseline_id, name, created_at),
            )
            self._conn.executemany(
                "INSERT INTO exchanges ("
                " baseline_id, position, exchange_id, recorded_at,"
                " req_method, req_url, req_path, req_headers, req_query, req_body, req_body_json,"
                " res_status, res_headers, res_body, res_body_json, res_elapsed_ms, tags, deps"
                ") VALUES (" + ",".join("?" * 18) + ")",
                ((baseline_id, i, *_exchange_to_columns(ex)) for i, ex in enumerate(rows)),
            )
        return baseline_id

    def get_baseline_id(self, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT baseline_id FROM baselines WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else str(row["baseline_id"])

    def list_baselines(self) -> list[BaselineInfo]:
        rows = self._conn.execute(
            "SELECT b.name, b.baseline_id, b.created_at, COUNT(e.exchange_id) AS n"
            " FROM baselines b LEFT JOIN exchanges e ON e.baseline_id = b.baseline_id"
            " GROUP BY b.baseline_id, b.name, b.created_at"
            " ORDER BY b.created_at DESC, b.rowid DESC"
        ).fetchall()
        return [
            BaselineInfo(
                name=str(r["name"]),
                baseline_id=str(r["baseline_id"]),
                created_at=_parse_dt(r["created_at"]),
                exchange_count=int(r["n"]),
            )
            for r in rows
        ]

    def load_baseline(self, baseline_id: str) -> list[RecordedExchange]:
        if self._conn.execute(
            "SELECT 1 FROM baselines WHERE baseline_id = ?", (baseline_id,)
        ).fetchone() is None:
            raise KeyError(f"未知 baseline_id: {baseline_id}")
        rows = self._conn.execute(
            "SELECT * FROM exchanges WHERE baseline_id = ? ORDER BY position ASC",
            (baseline_id,),
        ).fetchall()
        return [_row_to_exchange(r) for r in rows]

    def iter_baseline(self, baseline_id: str) -> Iterator[RecordedExchange]:
        """流式读取，供大基线使用；语义同 `load_baseline`。"""
        for exchange in self.load_baseline(baseline_id):
            yield exchange

    # -- 报告 --------------------------------------------------------------

    def save_report(self, report: ReplayReport) -> None:
        payload = json.dumps(_report_to_json(report), ensure_ascii=False)
        created_at = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT INTO reports (baseline_id, created_at, payload) VALUES (?, ?, ?)",
                (report.baseline_id, created_at, payload),
            )

    def latest_report(self, baseline_id: str | None = None) -> ReplayReport | None:
        if baseline_id is None:
            row = self._conn.execute(
                "SELECT payload FROM reports ORDER BY report_id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT payload FROM reports WHERE baseline_id = ? ORDER BY report_id DESC LIMIT 1",
                (baseline_id,),
            ).fetchone()
        if row is None:
            return None
        return _report_from_json(json.loads(row["payload"]))


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------


def _exchange_to_columns(ex: RecordedExchange) -> tuple[Any, ...]:
    req, res = ex.request, ex.response
    return (
        ex.exchange_id,
        ex.recorded_at.isoformat(),
        req.method,
        req.url,
        req.path,
        json.dumps(dict(req.headers), ensure_ascii=False),
        json.dumps({k: list(v) for k, v in req.query.items()}, ensure_ascii=False),
        req.body,
        _dump_opt_json(req.body_json),
        res.status,
        json.dumps(dict(res.headers), ensure_ascii=False),
        res.body,
        _dump_opt_json(res.body_json),
        res.elapsed_ms,
        json.dumps(dict(ex.tags), ensure_ascii=False),
        json.dumps(
            [{"kind": d.kind, "ref": d.ref, "scrubbed": d.scrubbed} for d in ex.deps],
            ensure_ascii=False,
        ),
    )


def _dump_opt_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _row_to_exchange(row: sqlite3.Row) -> RecordedExchange:
    request = RecordedRequest(
        method=str(row["req_method"]),
        url=str(row["req_url"]),
        path=str(row["req_path"]),
        headers=json.loads(row["req_headers"]),
        query=json.loads(row["req_query"]),
        body=None if row["req_body"] is None else bytes(row["req_body"]),
        body_json=_load_opt_json(row["req_body_json"]),
    )
    response = RecordedResponse(
        status=int(row["res_status"]),
        headers=json.loads(row["res_headers"]),
        body=None if row["res_body"] is None else bytes(row["res_body"]),
        body_json=_load_opt_json(row["res_body_json"]),
        elapsed_ms=None if row["res_elapsed_ms"] is None else float(row["res_elapsed_ms"]),
    )
    deps = tuple(
        Dependency(kind=str(d["kind"]), ref=str(d["ref"]), scrubbed=str(d["scrubbed"]))
        for d in json.loads(row["deps"])
    )
    return RecordedExchange(
        exchange_id=str(row["exchange_id"]),
        recorded_at=_parse_dt(row["recorded_at"]),
        request=request,
        response=response,
        tags=json.loads(row["tags"]),
        deps=deps,
    )


def _parse_dt(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _load_opt_json(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


def _report_to_json(report: ReplayReport) -> dict[str, Any]:
    return {
        "baseline_id": report.baseline_id,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "verdicts": [
            {
                "exchange_id": v.exchange_id,
                "replayed_at": v.replayed_at.isoformat(),
                "elapsed_ms": v.elapsed_ms,
                "matched_baseline": v.matched_baseline,
                "status_changed": v.status_changed,
                "error": v.error,
                "diffs": [
                    {
                        "path": d.path,
                        "level": str(d.level),
                        "expected": d.expected,
                        "actual": d.actual,
                        "reason": d.reason,
                    }
                    for d in v.diffs
                ],
            }
            for v in report.verdicts
        ],
    }


def _report_from_json(data: dict[str, Any]) -> ReplayReport:
    verdicts = tuple(
        ExchangeVerdict(
            exchange_id=str(v["exchange_id"]),
            replayed_at=_parse_dt(v["replayed_at"]),
            elapsed_ms=float(v.get("elapsed_ms") or 0.0),
            matched_baseline=bool(v.get("matched_baseline", True)),
            status_changed=bool(v.get("status_changed", False)),
            diffs=tuple(
                FieldDiff(
                    path=str(d["path"]),
                    level=DiffLevel(d["level"]),
                    expected=d.get("expected"),
                    actual=d.get("actual"),
                    reason=d.get("reason"),
                )
                for d in v.get("diffs", ())
            ),
            error=v.get("error"),
        )
        for v in data.get("verdicts", ())
    )
    return ReplayReport(
        baseline_id=str(data["baseline_id"]),
        started_at=_parse_dt(data["started_at"]),
        finished_at=_parse_dt(data["finished_at"]),
        verdicts=verdicts,
    )
