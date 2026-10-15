"""故障注入探针：用例**绕过工厂**造数据且不登记回收，用来验证会话级基线断言真的会红。

为什么它也必须失败、且必须放在非 `test_*.py` 的文件里：同 `_fault_probe.py`。
这条用例钉的是另一件事：函数级兜底只管"登记过的"数据，所以漏登记的数据
一定会漂到会话收尾 —— 那时由 `data_isolation_check` 的 `assert before == after` 抓住。
"清理不彻底能被现有断言抓到"这句话，靠的就是这条用例能被跑红。
"""

from __future__ import annotations


def test_unregistered_data_is_caught_by_session_isolation_check(factory, mes):
    """直接把请求发出去、不登记回收：函数级兜底抓不到它，会话级断言必须抓到。

    这是**故意留泄漏**的用例，断言在它身上没有意义 —— 它的价值全在"它跑完之后"。
    """
    order_no = f"WO-LEAK-{factory.tag()}"
    resp = mes.create_work_order(
        "PLANNER",
        product_code="FG-TEST-01",
        product_name="漏登记用例",
        planned_qty="1.000",
        order_no=order_no,
    )
    mes.expect_ok(resp, 201)
    # 故意不写 factory.register_order(order_no)：模拟"绕开工厂直接造数据"的真实失误。
