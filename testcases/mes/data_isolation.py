"""数据隔离的判定口径：把「有没有污染」变成一个可计算的量。

为什么单独放一个模块而不是写在 conftest 里：
`conftest.py` 里的 fixture 和这里被用例直接调用的函数，必须用**同一套口径**，
否则会出现"fixture 说干净、用例算出来脏"这种谁也说不清的局面。

两个指标：
1. 行数（row_counts）：抓"用例造了数据没收"。
2. 库存可用量合计（stock_totals）：抓"行数没变但数值被改了"。
   这一条是行数指标抓不到的 —— 领料/退料只 UPDATE 一行，行数完全不变，
   但账实已经不符。只断行数的隔离检查会在这里假绿。
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


def row_counts(db_path: Path) -> dict[str, int]:
    """直连 SQLite 取各表行数。

    为什么可以直连库：行数是"数据库物理事实"，服务端没有暴露 count 接口；
    而删除动作必须走接口，才能顺带验证服务端的业务约束（软删、状态机）。
    """
    with sqlite3.connect(str(db_path), timeout=10.0) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def stock_available_total(db_path: Path) -> int:
    """全部库存行的 available_milli 合计（整数，单位千分之一）。

    用合计而不是逐行比对：用例合法地领料又退料时，只要总量回到原值就算干净，
    中间过程不必留痕 —— 要求逐行一致会把正确的收尾动作也判成污染。
    """
    with sqlite3.connect(str(db_path), timeout=10.0) as conn:
        return int(conn.execute("SELECT COALESCE(SUM(available_milli), 0) FROM stock").fetchone()[0])


def snapshot(db_path: Path) -> dict:
    """一次拿全判定所需的两个指标。"""
    return {"counts": row_counts(db_path), "stock_available_total": stock_available_total(db_path)}


def describe_diff(before: dict, after: dict) -> str:
    """把差异整理成人能直接读的句子，作为断言失败信息。"""
    lines: list[str] = []
    for table, expected in before["counts"].items():
        actual = after["counts"][table]
        if actual != expected:
            lines.append(f"  表 {table}: 基线 {expected} 行 -> 现在 {actual} 行（差 {actual - expected:+d}）")
    before_qty, after_qty = before["stock_available_total"], after["stock_available_total"]
    if before_qty != after_qty:
        lines.append(
            f"  库存可用量合计: 基线 {before_qty / 1000:.3f} -> 现在 {after_qty / 1000:.3f}"
            f"（差 {(after_qty - before_qty) / 1000:+.3f}）"
        )
    return "\n".join(lines) if lines else "（无差异）"
