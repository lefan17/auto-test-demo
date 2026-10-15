"""运行度量的持久层：SQLite schema + 读写 API。

三张表：

===============  ==========================================================
``runs``         一轮运行一行（层、commit、起止、总/通过/失败/错误/跳过/xfail、通过率、结论）
``case_outcomes`` 用例级结果（一轮 x 一条用例），flaky 判定与失败归因的数据源
``contract_drift`` 契约漂移历史（一个 commit 一行）
===============  ==========================================================

设计取舍：

1. **纯 ``sqlite3``**，不引 SQLAlchemy。平台的读者是 CI 与人类，一次查询几行数据，
   ORM 带来的映射成本换不来任何东西。
2. **通过率的分母是「可判定用例」= passed + failed**，不含 skipped / error / xfailed。
   ``skipped`` 是「没跑」，``error`` 是「没跑成」（环境/夹具故障），把二者算进分母会让
   通过率同时反映「被测质量」与「环境健康度」，两个信号搅在一起就都读不出来了。
   环境问题走 ``errors`` 字段单独看，见 ``classify`` 与 report 的归因页。
3. ``--strict-markers`` 与 ``PRAGMA user_version`` 都用于**版本化**：schema 变更时
   ``migrate()`` 能判断该不该动手，而不是靠 ``CREATE TABLE IF NOT EXISTS`` 撞运气。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: 当前 schema 版本，写进 ``PRAGMA user_version``。
SCHEMA_VERSION = 1

#: 判定为「可判定」的用例结论：只有这两种参与通过率分母。
DECISIVE_OUTCOMES = ("passed", "failed")

#: 全部合法结论，与契约一致。
VALID_OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    layer             TEXT NOT NULL,
    commit_sha        TEXT,
    started_at        TEXT NOT NULL,
    duration_seconds  REAL NOT NULL,
    total             INTEGER NOT NULL,
    passed            INTEGER NOT NULL,
    failed            INTEGER NOT NULL,
    errors            INTEGER NOT NULL,
    skipped           INTEGER NOT NULL,
    xfailed           INTEGER NOT NULL,
    pass_rate         REAL NOT NULL,
    outcome           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_outcomes (
    run_id            TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    duration_seconds  REAL NOT NULL,
    PRIMARY KEY (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS contract_drift (
    commit_sha        TEXT PRIMARY KEY,
    checked_at        TEXT NOT NULL,
    drifted           INTEGER NOT NULL,
    changed           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_case_outcomes_case ON case_outcomes (case_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
"""


def utc_now() -> datetime:
    """当前 UTC 时间（tz-aware）。"""
    return datetime.now(UTC)


def format_dt(value: datetime) -> str:
    """统一的时间戳文本格式：ISO 8601，带 ``+00:00`` 偏移，秒级。
    带偏移是硬要求——不带偏移的 ``datetime`` 在跨时区读回来时会静默变成本地时间。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_dt(text: str) -> datetime:
    """``format_dt`` 的逆操作；历史数据里若混入过 ``Z`` 后缀也能读。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def new_run_id(layer: str, started_at: datetime | None = None) -> str:
    """生成可读且唯一的 run_id：``<layer>-<UTC 时间戳>-<随机后缀>``。

    时间戳让人在 SQL 里一眼看出先后，随机后缀保证同一秒内并发跑两层也不撞主键。
    """
    stamp = (started_at or utc_now()).astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{layer}-{stamp}-{uuid.uuid4().hex[:8]}"


def pass_rate_of(passed: int, failed: int, total: int) -> float:
    """通过率 = passed / (passed + failed)；没有可判定用例时返回 0.0（不算通过）。

    没有可判定用例却有 total（全 error / 全 skipped）说明这一轮什么结论都没产出，
    按 0.0 处理才安全：否则「全部 error」会得到 1.0 的满分通过率。
    """
    decisive = passed + failed
    if decisive <= 0:
        return 0.0
    return passed / decisive


def outcome_of(passed: int, failed: int, errors: int, total: int) -> str:
    """把计数压成一个结论字串，供 ``runs.outcome`` 与门禁快速判读。"""
    if errors and not (passed or failed):
        return "error"
    if total == 0:
        return "error"
    if failed or errors:
        return "failed"
    return "passed"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    layer: str
    commit_sha: str | None
    started_at: datetime
    duration_seconds: float
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    pass_rate: float
    outcome: str


@dataclass(frozen=True)
class CaseOutcome:
    run_id: str
    case_id: str
    outcome: str
    duration_seconds: float


@dataclass(frozen=True)
class DriftRecord:
    commit_sha: str
    checked_at: datetime
    drifted: bool
    changed: tuple[str, ...]


class MetricsStore:
    """``qaplat/.cache/qaplat.db`` 的读写封装。

    ``check_same_thread=False`` 是刻意开的口子：``gate`` 会在子进程跑 pytest 的同时
    从同一进程读库做增量刷新，pytest 的 xdist worker 也会拿到同一个 store 对象。
    单连接 + 串行调用，不引入连接池——写入频率是「一轮一次」，争用不是问题。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent and str(self.db_path.parent) not in ("", "."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MetricsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def migrate(self) -> None:
        """建表并写 schema 版本；幂等，重复调用无副作用。"""
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        self._conn.commit()

    # ------------------------------------------------------------------ 写入

    def record_run(self, run: RunRecord, cases: Sequence[CaseOutcome] = ()) -> None:
        """写入一轮运行及其用例级结果。

        以 ``run_id`` 为主键做替换，并对用例行先删后插：重复记录同一 run_id
        （比如重跑 gate 补数据）得到的是「最后一次写入的状态」，而不是主键冲突。
        """
        self._conn.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, layer, commit_sha, started_at, duration_seconds,
                total, passed, failed, errors, skipped, xfailed, pass_rate, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.layer,
                run.commit_sha,
                format_dt(run.started_at),
                float(run.duration_seconds),
                int(run.total),
                int(run.passed),
                int(run.failed),
                int(run.errors),
                int(run.skipped),
                int(run.xfailed),
                float(run.pass_rate),
                run.outcome,
            ),
        )
        if cases:
            self._conn.execute("DELETE FROM case_outcomes WHERE run_id = ?", (run.run_id,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO case_outcomes (run_id, case_id, outcome, duration_seconds) VALUES (?, ?, ?, ?)",
                [(c.run_id, c.case_id, c.outcome, float(c.duration_seconds)) for c in cases],
            )
        self._conn.commit()

    def record_drift(self, commit_sha: str, drifted: bool, changed: Sequence[str]) -> None:
        """记录一次契约漂移检查结果（同 commit 覆盖写）。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO contract_drift (commit_sha, checked_at, drifted, changed) VALUES (?, ?, ?, ?)",
            (commit_sha, format_dt(utc_now()), 1 if drifted else 0, json.dumps(list(changed), ensure_ascii=False)),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ 读取

    def recent_runs(self, limit: int = 10) -> list[RunRecord]:
        """最近 ``limit`` 轮运行，新的在前。

        排序用 ``started_at`` 再叠一个 ``rowid``：同一秒启动的两轮（例如 pr 与 nightly
        被同一个钩子触发）时间戳会完全相同，只按时间排会得到不稳定的顺序。
        """
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    def latest_run(self, layer: str | None = None) -> RunRecord | None:
        """最近一轮（可限定层），用于 ``report`` 与 ``gate`` 的快速取值。"""
        if layer is None:
            rows = self.recent_runs(1)
        else:
            rows = [
                _row_to_run(r)
                for r in self._conn.execute(
                    "SELECT * FROM runs WHERE layer = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                    (layer,),
                ).fetchall()
            ]
        return rows[0] if rows else None

    def run_case_outcomes(self, run_id: str) -> list[CaseOutcome]:
        """某一轮的用例级结果，按 case_id 稳定排序。"""
        rows = self._conn.execute(
            "SELECT run_id, case_id, outcome, duration_seconds FROM case_outcomes WHERE run_id = ? ORDER BY case_id",
            (run_id,),
        ).fetchall()
        return [
            CaseOutcome(
                run_id=r["run_id"],
                case_id=r["case_id"],
                outcome=r["outcome"],
                duration_seconds=float(r["duration_seconds"]),
            )
            for r in rows
        ]

    def flaky_cases(self, last_n_runs: int = 10) -> list[tuple[str, int, int]]:
        """跨轮次不稳定用例：返回 ``(case_id, 通过轮次, 出现轮次)``。

        「出现轮次」只数**可判定**的轮次（passed / failed），skipped 与 error 既不计入
        出现、也不计入通过——一条用例被跳过一轮、通过一轮，不是 flaky，是没跑。
        返回的只是「既通过过、也失败过」的用例（``0 < 通过轮次 < 出现轮次``）。
        """
        window = self.recent_runs(limit=last_n_runs)
        if not window:
            return []
        run_ids = [r.run_id for r in window]
        placeholders = ",".join("?" for _ in run_ids)
        rows = self._conn.execute(
            f"""
            SELECT case_id,
                   SUM(CASE WHEN outcome = 'passed' THEN 1 ELSE 0 END) AS passes,
                   COUNT(*) AS appearances
            FROM case_outcomes
            WHERE run_id IN ({placeholders}) AND outcome IN ('passed', 'failed')
            GROUP BY case_id
            HAVING passes > 0 AND passes < COUNT(*)
            ORDER BY (COUNT(*) - SUM(CASE WHEN outcome = 'passed' THEN 1 ELSE 0 END)) DESC, case_id ASC
            """,
            run_ids,
        ).fetchall()
        return [(r["case_id"], int(r["passes"]), int(r["appearances"])) for r in rows]

    def contract_drift_history(self, limit: int = 20) -> list[DriftRecord]:
        """契约漂移历史，新的在前。"""
        rows = self._conn.execute(
            "SELECT commit_sha, checked_at, drifted, changed FROM contract_drift ORDER BY checked_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            DriftRecord(
                commit_sha=r["commit_sha"],
                checked_at=parse_dt(r["checked_at"]),
                drifted=bool(r["drifted"]),
                changed=tuple(json.loads(r["changed"])),
            )
            for r in rows
        ]

    def layer_summary(self, last_n_runs: int = 20) -> list[tuple[str, int, float, float]]:
        """按层聚合：``(layer, 轮次, 平均通过率, 平均耗时秒)``，给 report 用。"""
        rows = self._conn.execute(
            """
            SELECT layer, COUNT(*) AS n, AVG(pass_rate) AS rate, AVG(duration_seconds) AS dur
            FROM (SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?)
            GROUP BY layer ORDER BY layer
            """,
            (int(last_n_runs),),
        ).fetchall()
        return [(r["layer"], int(r["n"]), float(r["rate"] or 0.0), float(r["dur"] or 0.0)) for r in rows]


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        layer=row["layer"],
        commit_sha=row["commit_sha"],
        started_at=parse_dt(row["started_at"]),
        duration_seconds=float(row["duration_seconds"]),
        total=int(row["total"]),
        passed=int(row["passed"]),
        failed=int(row["failed"]),
        errors=int(row["errors"]),
        skipped=int(row["skipped"]),
        xfailed=int(row["xfailed"]),
        pass_rate=float(row["pass_rate"]),
        outcome=row["outcome"],
    )


def build_run_record(
    *,
    run_id: str,
    layer: str,
    commit_sha: str | None,
    started_at: datetime,
    duration_seconds: float,
    counts: "dict[str, int]",
) -> RunRecord:
    """由计数表拼出 ``RunRecord``，通过率与结论按本模块的统一口径推导。

    ``counts`` 的键是结论名（passed/failed/error/skipped/xfailed），未知键忽略。
    """
    picked: dict[str, int] = {name: int(counts.get(name, 0)) for name in VALID_OUTCOMES}
    total = sum(picked.values()) if counts.get("total") is None else int(counts["total"])
    passed, failed, errors = picked["passed"], picked["failed"], picked["error"]
    return RunRecord(
        run_id=run_id,
        layer=layer,
        commit_sha=commit_sha,
        started_at=started_at,
        duration_seconds=float(duration_seconds),
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=picked["skipped"],
        xfailed=picked["xfailed"],
        pass_rate=pass_rate_of(passed, failed, total),
        outcome=outcome_of(passed, failed, errors, total),
    )


def case_outcomes_from_pairs(run_id: str, pairs: Iterable[tuple[str, str, float]]) -> list[CaseOutcome]:
    """``(case_id, outcome, duration)`` 三元组转 ``CaseOutcome``，顺带校验结论合法性。"""
    out: list[CaseOutcome] = []
    for case_id, outcome, duration in pairs:
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"非法用例结论：{outcome!r}（合法值：{', '.join(VALID_OUTCOMES)}）")
        out.append(CaseOutcome(run_id=run_id, case_id=case_id, outcome=outcome, duration_seconds=float(duration)))
    return out
