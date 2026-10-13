"""数据隔离的判定口径：把「有没有污染」变成一个可计算的量。

为什么单独放一个模块而不是写在 conftest 里：
`conftest.py` 里的 fixture 和这里被用例直接调用的函数，必须用**同一套口径**，
否则会出现"fixture 说干净、用例算出来脏"这种谁也说不清的局面。

三个指标：
1. 行数（row_counts）：抓"用例造了数据没收"。
2. 关键数值列合计（column_totals）：抓"行数没变但数值被改了"。
   领料/退料只 UPDATE 一行，行数完全不变，但账实已经不符 ——
   只断行数的隔离检查会在这里假绿。
3. 流水余额链（ledger_balance_chain_errors）：抓"写进流水的金额算错"。
   第 2 条指标抓不到它：流水行会被用例的回收逻辑删掉，
   会话收尾的快照里根本没有留下可比的数字（实测流水表在会话结束时是 0 行），
   所以这条不变量必须在**行还在的时候**验（挂在 factory 回收之前）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

TABLES = (
    "users",
    "materials",
    "work_orders",
    "production_batches",
    "inspections",
    "stock",
    "stock_ledger",
    "idempotency_keys",
)

# 关键数值列：只改这些列不改行数，就是最阴的一类数据泄漏。
# (表, 列, 展示名) —— 展示名只用于断言失败信息。
COLUMN_TOTALS: tuple[tuple[str, str, str], ...] = (
    ("stock", "available_milli", "库存可用量合计"),
    ("stock", "reserved_milli", "库存占用合计"),
    ("stock", "issued_milli", "已发料合计"),
    ("stock_ledger", "qty_milli", "流水数量合计"),
    ("stock_ledger", "balance_milli", "流水账面余额合计"),
)

TOTAL_LABELS = {f"{table}.{column}": label for table, column, label in COLUMN_TOTALS}

# 流水类型与账面余额变化方向：记余额的规则见 mock_server/services.py 的 _set_ledger_operator。
SIGNED_TXN = {"RECEIPT": 1, "REVERSE": 1, "ISSUE": -1}


def row_counts(db_path: Path) -> dict[str, int]:
    """直连 SQLite 取各表行数。

    为什么可以直连库：行数是"数据库物理事实"，服务端没有暴露 count 接口；
    而删除动作必须走接口，才能顺带验证服务端的业务约束（软删、状态机）。
    """
    with sqlite3.connect(str(db_path), timeout=10.0) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def column_totals(db_path: Path) -> dict[str, int]:
    """关键数值列的合计，键的形状是 "表.列"（整数，数量单位为千分之一）。

    用合计而不是逐行比对：用例合法地领料又退料时，只要总量回到原值就算干净，
    中间过程不必留痕 —— 要求逐行一致会把正确的收尾动作也判成污染。
    """
    with sqlite3.connect(str(db_path), timeout=10.0) as conn:
        return {
            f"{table}.{column}": int(conn.execute(f"SELECT COALESCE(SUM({column}), 0) FROM {table}").fetchone()[0])
            for table, column, _ in COLUMN_TOTALS
        }


def stock_available_total(db_path: Path) -> int:
    """全部库存行的 available_milli 合计（单列口径，便于单独引用）。"""
    return column_totals(db_path)["stock.available_milli"]


def snapshot(db_path: Path) -> dict:
    """一次拿全判定所需的指标。"""
    return {"counts": row_counts(db_path), "totals": column_totals(db_path)}


def ledger_balance_chain_errors(db_path: Path) -> list[str]:
    """逐条重放流水，检查每条记录的 balance_milli 是否等于"当时的真实账面余额"。

    判据（同一 (物料, 仓库) 上）：
    第 i 条的 balance 必须等于第 i-1 条的 balance 加上本条的**带符号数量**
    （ISSUE 为负，RECEIPT / REVERSE 为正）；并且最后一条的余额必须等于
    `stock.available_milli`。

    期初值不靠硬编码种子，而是从两处事实反推：
        期初 = 当前可用量 - 全部流水的带符号净变化
    这样不管用例新建了多少物料、造了多少流水，判据都自洽。

    为什么这条检查必须存在、以及为什么不能交给会话收尾的快照：
    流水表的关键金额一旦只被 UPDATE（行数不变），只断行数的隔离检查完全看不见；
    更糟的是 `data_factory.cleanup()` 会把用例自己造的流水行删掉，
    等会话收尾取快照时流水表已经是空的 —— 任何写进流水的错账都无从比对。
    所以这条不变量必须在**流水行还活着的时候**（回收之前）验。
    """
    problems: list[str] = []
    with sqlite3.connect(str(db_path), timeout=10.0) as conn:
        rows = conn.execute(
            "SELECT id, material_code, warehouse, txn_type, qty_milli, balance_milli FROM stock_ledger ORDER BY id ASC"
        ).fetchall()
        available = {
            (material_code, warehouse): available_milli
            for material_code, warehouse, available_milli in conn.execute(
                "SELECT material_code, warehouse, available_milli FROM stock"
            )
        }

    net_change: dict[tuple[str, str], int] = {}
    unknown_txn: list[str] = []
    for _, material_code, warehouse, txn_type, qty_milli, _ in rows:
        if txn_type not in SIGNED_TXN:
            unknown_txn.append(f"  流水出现了未登记方向的类型 {txn_type}（{material_code}/{warehouse}）")
            continue
        key = (material_code, warehouse)
        net_change[key] = net_change.get(key, 0) + SIGNED_TXN[txn_type] * qty_milli
    problems.extend(unknown_txn)

    running = {key: available.get(key, 0) - net_change.get(key, 0) for key in set(available) | set(net_change)}
    for row_id, material_code, warehouse, txn_type, qty_milli, balance_milli in rows:
        if txn_type not in SIGNED_TXN:
            continue
        key = (material_code, warehouse)
        expected = running.get(key, 0) + SIGNED_TXN[txn_type] * qty_milli
        if balance_milli != expected:
            problems.append(
                f"  流水 #{row_id} {material_code}/{warehouse} {txn_type} {qty_milli / 1000:.3f}："
                f"记录余额 {balance_milli / 1000:.3f}，按链重放应为 {expected / 1000:.3f}"
                f"（差 {(balance_milli - expected) / 1000:+.3f}）"
            )
        running[key] = expected

    for key, available_milli in available.items():
        if running.get(key, 0) != available_milli:
            problems.append(
                f"  {key[0]}/{key[1]}：流水末笔余额 {running.get(key, 0) / 1000:.3f}"
                f" 与库存账面 {available_milli / 1000:.3f} 不一致"
                f"（差 {(running.get(key, 0) - available_milli) / 1000:+.3f}）—— 账实已不符"
            )
    return problems


def describe_diff(before: dict, after: dict) -> str:
    """把差异整理成人能直接读的句子，作为断言失败信息。"""
    lines: list[str] = []
    for table, expected in before["counts"].items():
        actual = after["counts"][table]
        if actual != expected:
            lines.append(f"  表 {table}: 基线 {expected} 行 -> 现在 {actual} 行（差 {actual - expected:+d}）")
    for key, expected in before["totals"].items():
        actual = after["totals"][key]
        if actual != expected:
            lines.append(
                f"  {TOTAL_LABELS.get(key, key)}: 基线 {expected / 1000:.3f} -> 现在 {actual / 1000:.3f}"
                f"（差 {(actual - expected) / 1000:+.3f}）"
            )
    return "\n".join(lines) if lines else "（无差异）"
