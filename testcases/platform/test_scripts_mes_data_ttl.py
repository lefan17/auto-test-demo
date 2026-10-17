"""TTL 清理脚本的退出码与删除口径（`scripts/mes_data_ttl.py`）。

为什么这些用例必须存在
----------------------
这个脚本的四个退出码此前**只写在文档里**，没有任何测试读它：
`README.md:509-512` 承诺 ``--apply`` 真删、``--fail-on-lingering`` 清完仍有残留则**退出码 3**；
`docs/wave3-data-lifecycle-handoff.md:163` 列了完整参数表，`:259`/`:270` 给了实测命令与
``exit=0``。文档不会变红，代码会 —— 把承诺钉在测试里，才不会在重构时悄悄变成别的退出码。

库怎么造：直接用 ``mock_server.db.SCHEMA_SQL`` 在 ``tmp_path`` 下建一份。
刻意**不**走 ``mock_db.configure()`` / monkeypatch ``_DB_PATH`` —— 那是进程内全局状态，
别的用例正在用它连自己的库，改完漏还原就会让后面的人连错库（正是本脚本要清理的那类污染）。

TTL 边界语义（`testcases/mes/data_ttl.py:405` + `:86`）
-----------------------------------------------------
``cutoff = now - ttl_hours``，判定条件是 ``stamp < cutoff``（**严格小于**）。
所以 ``created_at`` 恰好等于 ``now - 24h`` 的行**不删**。这条用固定 ``now`` 跑
``purge()`` 钉住，不依赖真实时钟的毫秒级抖动。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from mock_server.db import SCHEMA_SQL

pytestmark = pytest.mark.platform

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mes_data_ttl.py"


def _load_script() -> ModuleType:
    """按路径加载被测脚本。

    不能写 ``from scripts import mes_data_ttl``：``scripts/`` 没有 ``__init__.py``，
    那样同一个文件会以 ``mes_data_ttl`` 与 ``scripts.mes_data_ttl`` 两个模块名被 mypy 发现，
    直接以 exit 2 中止 —— ``error: Source file found twice under different module names``。
    """
    spec = importlib.util.spec_from_file_location("_mes_data_ttl_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ttl = _load_script()
# 退出码契约必须真走 `main()`，所以把它绑成本地名字。
main = ttl.main

# 远早于任何 TTL 的时间戳：用它造「过期」。
OLD = "2020-01-01T00:00:00+08:00"
# 刚刚：用它造「没过期」。
FRESH = "2999-01-01T00:00:00+08:00"

# 种子物料（在 data_factory.SEED_MATERIALS 里 → 硬白名单，永不删除）。
BASELINE_MATERIAL = "MAT-CU-001"
BASELINE_WAREHOUSE = "WH-01"
# 工厂形状（`^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[A-Z0-9]{6}-\d+$`）→ 认得出是测试数据。
TEST_MATERIAL = "MAT-T-ABC123-1"
TEST_ORDER = "WO-T-XYZ789-1"
# 过期但命名认不出来 → 只报告、不删（data_ttl.py:194-195 的 kept 分支）。
# 注意别写成 ``WO-MANUAL-001``：``MANUAL`` 恰好 6 个字符，会命中 FACTORY_TAG_RE
# 的 ``-[A-Z0-9]{6}-\d+`` 尾部，被当成工厂造的测试数据。日期形状 ``WO-2026-0001``
# 的第二段只有 4 个字符，才真的落在「认不出来」那一侧。
MANUAL_ORDER = "WO-2026-0001"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=10.0)


def _exec(path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    conn = _connect(path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _table_counts(path: Path) -> dict[str, int]:
    tables = (
        "users",
        "materials",
        "work_orders",
        "production_batches",
        "inspections",
        "stock",
        "stock_ledger",
        "idempotency_keys",
    )
    conn = _connect(path)
    try:
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}
    finally:
        conn.close()


@pytest.fixture
def ttl_db(tmp_path: Path) -> Path:
    """一份带种子的临时库：基线行 + 过期测试行 + 过期人工行 + 一条未过期行。"""
    path = tmp_path / "ttl.db"
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO users (username, password_hash, role, display_name, created_at)"
            " VALUES ('admin', 'x', 'ADMIN', '系统管理员', ?)",
            (OLD,),
        )
        # 基线物料 + 基线库存：过期也不许删（白名单是硬否决）。
        conn.execute(
            "INSERT INTO materials (material_code, name, category, unit, unit_price_cent,"
            " safety_stock_milli, created_at, updated_at) VALUES (?, '电解铜', 'RAW', 'KG', 100, 0, ?, ?)",
            (BASELINE_MATERIAL, OLD, OLD),
        )
        conn.execute(
            "INSERT INTO stock (material_code, warehouse, available_milli, updated_at) VALUES (?, ?, 120000, ?)",
            (BASELINE_MATERIAL, BASELINE_WAREHOUSE, OLD),
        )
        # 过期测试物料 + 它的库存行：都该被删。
        conn.execute(
            "INSERT INTO materials (material_code, name, category, unit, unit_price_cent,"
            " safety_stock_milli, created_at, updated_at) VALUES (?, '测试料', 'TEST', 'KG', 1, 0, ?, ?)",
            (TEST_MATERIAL, OLD, OLD),
        )
        conn.execute(
            "INSERT INTO stock (material_code, warehouse, available_milli, updated_at) VALUES (?, 'WH-09', 1000, ?)",
            (TEST_MATERIAL, OLD),
        )
        # 过期测试工单 + 批次 + 流水：按父行判定一起删。
        cursor = conn.execute(
            "INSERT INTO work_orders (order_no, product_code, product_name, planned_qty_milli,"
            " created_at, updated_at) VALUES (?, 'MAT-FG-ABC123-1', '测试产品', 10000, ?, ?)",
            (TEST_ORDER, OLD, OLD),
        )
        order_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO production_batches (batch_no, work_order_id, planned_qty_milli, line, operator,"
            " created_at, updated_at) VALUES ('B-T-1', ?, 10000, 'L1', '测试员', ?, ?)",
            (order_id, OLD, OLD),
        )
        conn.execute(
            "INSERT INTO stock_ledger (material_code, warehouse, txn_type, qty_milli, balance_milli,"
            " work_order_no, operator, created_at) VALUES ('MAT-FG-ABC123-1', 'WH-FG', 'RECEIPT',"
            " 10000, 10000, ?, '测试员', ?)",
            (TEST_ORDER, OLD),
        )
        # 过期但命名认不出来：报告出来，不动。
        conn.execute(
            "INSERT INTO work_orders (order_no, product_code, product_name, planned_qty_milli,"
            " created_at, updated_at) VALUES (?, 'MAT-CU-001', '人工演示单', 1000, ?, ?)",
            (MANUAL_ORDER, OLD, OLD),
        )
        # 没过期的测试工单：不该删。
        conn.execute(
            "INSERT INTO work_orders (order_no, product_code, product_name, planned_qty_milli,"
            " created_at, updated_at) VALUES ('WO-T-NEW111-2', 'MAT-FG-ABC123-1', '新单', 1000, ?, ?)",
            (FRESH, FRESH),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def clean_db(tmp_path: Path) -> Path:
    """只有基线行的库：清完之后不该有任何残留（``--fail-on-lingering`` 的阴性对照）。"""
    path = tmp_path / "clean.db"
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO users (username, password_hash, role, display_name, created_at)"
            " VALUES ('admin', 'x', 'ADMIN', '系统管理员', ?)",
            (OLD,),
        )
        conn.execute(
            "INSERT INTO materials (material_code, name, category, unit, unit_price_cent,"
            " safety_stock_milli, created_at, updated_at) VALUES (?, '电解铜', 'RAW', 'KG', 100, 0, ?, ?)",
            (BASELINE_MATERIAL, OLD, OLD),
        )
        conn.execute(
            "INSERT INTO stock (material_code, warehouse, available_milli, updated_at) VALUES (?, ?, 120000, ?)",
            (BASELINE_MATERIAL, BASELINE_WAREHOUSE, OLD),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_dry_run_deletes_nothing(ttl_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """README.md:509 承诺的默认档是试运行：报告要删什么，但一行都不动、退出码 0。"""
    before = _table_counts(ttl_db)

    code = main(["--db", str(ttl_db), "-n", "24"])

    out = capsys.readouterr().out
    assert code == 0
    assert "试运行" in out
    assert "命中的过期测试数据" in out
    assert _table_counts(ttl_db) == before, "dry-run 改了数据 —— 安全默认已经被破坏"


def test_apply_removes_expired_test_rows_only(ttl_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """README.md:511 的 ``--apply``：真删，且只删过期测试数据，基线一行不动。"""
    before = _table_counts(ttl_db)

    code = main(["--db", str(ttl_db), "-n", "24", "--apply"])

    out = capsys.readouterr().out
    after = _table_counts(ttl_db)
    assert code == 0
    assert "已执行删除" in out
    # 过期测试物料与它那一行库存都走。
    assert after["materials"] == before["materials"] - 1
    assert after["stock"] == before["stock"] - 1
    # 工单只少了那一张测试单；批次与流水按父行判定一起走。
    assert after["work_orders"] == before["work_orders"] - 1
    assert after["production_batches"] == before["production_batches"] - 1
    assert after["stock_ledger"] == before["stock_ledger"] - 1
    # 基线（种子用户 / 种子物料 / 种子库存）与未过期行都还在。
    assert after["users"] == before["users"]
    assert after["materials"] == 1
    assert after["stock"] == 1

    conn = _connect(ttl_db)
    try:
        remaining = {str(row[0]) for row in conn.execute("SELECT order_no FROM work_orders").fetchall()}
        ledger_owner = conn.execute("SELECT COUNT(*) FROM stock_ledger").fetchone()[0]
        baseline_kept = int(
            conn.execute("SELECT COUNT(*) FROM materials WHERE material_code = ?", (BASELINE_MATERIAL,)).fetchone()[0]
        )
    finally:
        conn.close()
    assert remaining == {MANUAL_ORDER, "WO-T-NEW111-2"}, "删错了工单：过期测试单没删干净，或未过期/人工单被误删"
    assert ledger_owner == 0
    assert baseline_kept == 1


def test_fail_on_lingering_exits_3_when_residue_remains(ttl_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """README.md:512 承诺：清完仍有残留 → **退出码 3**。残留是那张过期的人工单。"""
    code = main(["--db", str(ttl_db), "-n", "24", "--apply", "--fail-on-lingering"])

    captured = capsys.readouterr()
    assert code == 3, f"--fail-on-lingering 没有以退出码 3 结束（实际 {code}），承诺已失效"
    assert "非基线残留" in captured.err
    assert "退出码 3" in captured.err


def test_fail_on_lingering_exits_0_when_database_is_clean(clean_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """阴性对照：库本来没有残留时，同一个开关必须返回 0 —— 否则退出码 3 毫无信息量。"""
    code = main(["--db", str(clean_db), "-n", "24", "--apply", "--fail-on-lingering"])

    captured = capsys.readouterr()
    assert code == 0, f"干净的库被判成了有残留（退出码 {code}）"
    assert "非基线残留：无" in captured.out


def test_json_output_is_parseable_and_complete(ttl_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """docs/wave3-data-lifecycle-handoff.md:163 的 ``--json``：CI 要消费它，形状必须稳定。"""
    code = main(["--db", str(ttl_db), "-n", "24", "--json"])

    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)  # 解析不了就是这条先红
    assert set(payload) == {
        "db",
        "ttl_hours",
        "cutoff",
        "applied",
        "planned_rows",
        "items",
        "baseline_kept",
        "baseline_lost",
        "lingering",
        "unidentified",
        "row_counts_before",
        "row_counts_after",
    }
    assert payload["db"] == str(ttl_db)
    assert payload["ttl_hours"] == 24.0
    assert payload["applied"] is False
    assert payload["planned_rows"] == sum(item["rows"] for item in payload["items"])
    assert payload["planned_rows"] > 0, "这份库里有过期测试数据，--json 却报告 0 行"
    assert {"table", "key", "rows", "reason"} == set(payload["items"][0])
    assert payload["unidentified"], "过期但命名认不出来的行必须被列出来，不能静默留着"
    assert payload["row_counts_before"] == payload["row_counts_after"], "dry-run 的 --json 也报了行数变化"


def test_list_lingering_reports_without_deleting(ttl_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--list-lingering`` 只看残留（忽略 TTL），不删任何东西。"""
    before = _table_counts(ttl_db)

    code = main(["--db", str(ttl_db), "--list-lingering"])

    out = capsys.readouterr().out
    assert code == 0
    assert "非基线残留" in out
    assert MANUAL_ORDER in out, "人工造的过期单没被列出来"
    assert _table_counts(ttl_db) == before


def test_list_lingering_with_fail_flag_exits_3(ttl_db: Path) -> None:
    """``--list-lingering --fail-on-lingering`` 也走退出码 3（脚本 :87 的返回值）。"""
    assert main(["--db", str(ttl_db), "--list-lingering", "--fail-on-lingering"]) == 3


def test_purge_is_idempotent(ttl_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """第二次 ``--apply`` 不该找到任何可删项，也不该报错（data_ttl.py:396-398）。"""
    assert main(["--db", str(ttl_db), "-n", "24", "--apply"]) == 0
    capsys.readouterr()

    code = main(["--db", str(ttl_db), "-n", "24", "--apply"])

    out = capsys.readouterr().out
    assert code == 0
    assert "没有命中的过期测试数据" in out
    assert "行数未变化" in out


def test_row_exactly_at_the_ttl_boundary_is_kept(tmp_path: Path) -> None:
    """边界语义：``cutoff = now - ttl`` 且判定是 ``stamp < cutoff``（data_ttl.py:405、:86）。

    恰好等于 24 小时的行**不删**，早一秒才删。用固定 ``now`` 跑 ``purge()``，
    不依赖真实时钟 —— 否则「恰好」这两个字每跑一次都差几微秒。
    """
    path = tmp_path / "boundary.db"
    now = datetime.fromisoformat("2026-03-01T12:00:00+08:00")
    exact = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    just_over = (now - timedelta(hours=24, seconds=1)).isoformat(timespec="seconds")

    conn = _connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        for order_no, created_at in (("WO-T-EXACT1-1", exact), ("WO-T-OVER11-1", just_over)):
            conn.execute(
                "INSERT INTO work_orders (order_no, product_code, product_name, planned_qty_milli,"
                " created_at, updated_at) VALUES (?, 'MAT-FG-ABC123-1', '边界单', 1000, ?, ?)",
                (order_no, created_at, created_at),
            )
        conn.commit()
    finally:
        conn.close()

    report = ttl.purge(path, 24, apply=True, now=now)

    conn = _connect(path)
    try:
        survivors = {str(row[0]) for row in conn.execute("SELECT order_no FROM work_orders").fetchall()}
    finally:
        conn.close()
    assert survivors == {"WO-T-EXACT1-1"}, f"TTL 边界语义变了：{survivors}（恰好 24h 的应当保留）"
    assert report.cutoff == now - timedelta(hours=24)
    assert [item.table for item in report.items] == ["work_orders"]
    assert report.applied is True
