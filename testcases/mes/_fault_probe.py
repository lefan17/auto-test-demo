"""故障注入探针：让流水链断言失败，验证「回收被断言挡掉」这个真实缺陷不再发生。

为什么不是"正文随便抛个异常"就算：
试过那种写法 —— 它在**修复前**也是绿的。原因很直白：用例正文抛异常根本不影响
fixture 的 teardown 往下走，`cleanup()` 照样执行。真正会挡住 `cleanup()` 的是
fixture 内部的顺序：早先是

    chain_errors = ledger_balance_chain_errors(...)
    assert not chain_errors          # ← 这里一红
    actions = f.cleanup()            # ← 这行就永远走不到

所以探针必须**精确命中那条断言**，否则它只是在证明"异常会传播"这种无关的事。
（这个错我犯过一次：第一版探针用正文抛异常，对着修复前的代码也是绿的，
等于什么都没证明。）

构造方式：用基线物料领料 + 退料（把库存数字原样还原，只留流水行），登记回收，
再直连库把流水的 `balance_milli` 改错 —— `ledger_balance_chain_errors()` 因此非空。于是：
- 修复前：断言先红 → cleanup 不执行 → 物料库存/流水/工单全留在库里 → 会话收尾连带红；
- 修复后：cleanup 先执行 → 数据回收干净 → 只剩流水链这一条断言红（真正的原因）。
"""

from __future__ import annotations

import sqlite3

SEED_MATERIAL = "MAT-CU-001"
SEED_WAREHOUSE = "WH-01"


def test_ledger_chain_failure_does_not_starve_cleanup(factory, mes, mes_server):
    order_no, _ = factory.work_order()

    issued = mes.issue(
        "OPERATOR",
        material_code=SEED_MATERIAL,
        qty="3.000",
        warehouse=SEED_WAREHOUSE,
        work_order_no=order_no,
    )
    mes.expect_ok(issued)
    factory.register_ledger_owner(order_no)

    # 退料把库存数字还原（领料只改数值不改行数），退料流水归属空串，一并登记。
    reversed_ = mes.as_role("OPERATOR").post(
        "/inventory/reverse",
        json={"material_code": SEED_MATERIAL, "qty": "3.000", "warehouse": SEED_WAREHOUSE},
        headers={"X-Work-Order-No": order_no},
    )
    mes.expect_ok(reversed_)
    factory.register_ledger_owner("")

    # 把流水余额改错：账实不符，流水链断言必然失败。
    conn = sqlite3.connect(str(mes_server.db_path), timeout=10.0)
    try:
        conn.execute(
            "UPDATE stock_ledger SET balance_milli = balance_milli + 999000 WHERE material_code = ?",
            (SEED_MATERIAL,),
        )
        conn.commit()
    finally:
        conn.close()
