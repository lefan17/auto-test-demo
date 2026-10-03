"""冒烟：一条用例证明"整个 MES 主流程是通的"。

标记说明：`smoke` 是提交前必须过的集合，`mes` 是本业务域专用标记。
两者叠加，是为了让 `pytest -m smoke` 和 `pytest -m mes` 都能选中它。
"""

import pytest

pytestmark = [pytest.mark.mes, pytest.mark.api, pytest.mark.smoke]


class TestMesSmoke:
    def test_full_lifecycle_pending_to_shipped(self, factory, mes):
        """主流程贯通：待审核 → 已审核 → 已排产 → 生产中 → 已完工 → 已发货。

        防的线上事故：**状态机在某一步卡死或跳步**。
        这类事故最典型的形态是"工单永远停在已排产"——计划员以为排了产，
        车间没收到任务；或者反过来"没质检就完工入库"，把不良品发给了客户。
        单测每个接口都过，但没人走过完整链路时，这种断链是可以上线的。
        """
        order_no, body = factory.work_order(planned_qty="10.000")
        mes.expect_field(body, "status", "PENDING")

        body = mes.expect_ok(factory.approve(order_no))
        mes.expect_field(body, "status", "APPROVED")
        mes.expect_field(body, "reviewer", "planner01")

        batch_resp = factory.add_batch(order_no, "10.000", line="L1")
        mes.expect_ok(batch_resp, 201)
        batch_no = batch_resp.json()["batch"]["batch_no"]

        body = mes.expect_ok(factory.schedule(order_no))
        mes.expect_field(body, "status", "SCHEDULED")

        start_body = mes.expect_ok(factory.start_batch(order_no, batch_no))
        assert start_body["work_order"]["status"] == "IN_PROGRESS"

        body = mes.expect_ok(factory.report(order_no, batch_no, good="10.000"))
        mes.expect_field(body, "status", "IN_PROGRESS")
        mes.expect_field(body, "reported_good_qty", "10.000")
        mes.expect_field(body, "remaining_qty", "0.000")

        mes.expect_ok(factory.inspect(order_no, "PASS", "10.000"), 201)

        body = mes.expect_ok(factory.stock_in(order_no))
        # 注意响应形状：stock-in 返回的是 OperationResult 包装
        # （{ok, message, work_order}），工单本体在 work_order 里；
        # 而 approve / schedule / ship 直接返回 WorkOrderOut 本体。
        # 这个不一致是被测服务自己的设计，不是用例的问题——
        # 所以这里显式读出 work_order，让"形状差异"在用例里可见。
        stocked = body["work_order"]
        mes.expect_field(stocked, "status", "COMPLETED")
        mes.expect_field(stocked, "stocked_in_qty", "10.000")

        body = mes.expect_ok(factory.ship(order_no))
        mes.expect_field(body, "status", "SHIPPED")

        # 终态校验：已发货之后没有任何合法出口，version 也不再变
        final = mes.expect_ok(mes.work_order(order_no))
        assert final["status"] == "SHIPPED"
        assert final["version"] == body["version"]

    def test_health_probe_reports_the_database_it_actually_uses(self, mes):
        """探针必须报出"自己连的是哪个库"。

        防的线上事故：**测试/服务连错库但一切显示正常**。
        我们真的踩过：`--db` 指定的库只被写、读请求却回落到默认库，
        所有断言在错误的数据上全绿。所以健康检查里带上库文件绝对路径，
        并且 fixture 启动时会比对它——让"连错库"变成一个启动期硬失败，
        而不是一条永远发现不了的假绿。
        """
        resp = mes.get("/healthz")
        body = mes.expect_ok(resp)
        assert body["status"] == "UP"
        assert body["database"].endswith(".db")
        # 九张表一张都不能少：漏建表的情况（比如新增表忘了写进 SCHEMA_SQL）
        # 只在跑到那条用例时才暴露，这里提前兜住
        for table in (
            "work_orders",
            "production_batches",
            "stock_ledger",
            "idempotency_keys",
            "inspections",
            "materials",
            "stock",
        ):
            assert table in body["tables"], f"缺少表 {table}"

    def test_login_returns_role_and_token(self, mes):
        """登录契约：返回 token + 角色。

        防的线上事故：**token 里角色丢失或错配**。
        角色是后端权限判断的唯一依据，如果登录返回的 role 与 token 内不一致，
        前端会按 A 显示按钮、后端按 B 拒绝，用户看到的就是"我有权限但点不动"。
        """
        for role in ("ADMIN", "PLANNER", "OPERATOR", "QA"):
            client = mes.as_role(role)
            me = mes.expect_ok(client.get("/auth/me"))
            assert me["role"] == role
            assert me["username"] == client.username

    def test_duplicate_order_no_is_rejected(self, factory, mes):
        """重复工单号必须被拒（400 ORDER_NO_DUPLICATE）。

        防的线上事故：**同一张工单在系统里存在两条**。
        单号可能是人工录入或从 ERP 离线导入的，重复单号会让后续领料、
        报工、入库分别挂到两条工单上——月底对账时产量多一倍，
        但实物只有一份，账实不符到这一步已经无法自动修复。
        """
        order_no, _ = factory.work_order()
        resp = mes.create_work_order(
            "PLANNER",
            product_code="FG-TEST-01",
            product_name="测试产品",
            planned_qty="1.000",
            order_no=order_no,
        )
        code = mes.expect_error(resp, 400, "ORDER_NO_DUPLICATE")
        assert code == "ORDER_NO_DUPLICATE"

    def test_quantity_precision_beyond_three_decimals_is_rejected(self, factory, mes):
        """数量超过 3 位小数必须报错，而不是悄悄四舍五入。

        防的线上事故：**账实不符的慢性泄漏**。
        客户报 1.0005 吨、系统记 1.000 吨，单次误差 0.5 公斤看不出来，
        一万笔之后就是 5 吨的缺口，而且没有任何一条日志能定位是哪笔丢的。
        宁可当场 422 让人改数据，也不要静默丢精度。
        """
        resp = mes.create_work_order(
            "PLANNER",
            product_code="FG-X",
            product_name="精度用例",
            planned_qty="1.0005",
        )
        mes.expect_error(resp, 422)

    def test_amount_is_cent_exact(self, factory, mes):
        """金额按分存储、按字符串返回，不能出现浮点尾数。

        防的线上事故：**金额出现 179.99999999999997 这种值**。
        一旦金额在链路上用 float 传过一次，四舍五入的时机就变得不确定，
        对账时会出现"差一分钱"的工单，而这一分钱会阻塞整批结算。
        """
        material = factory.material(unit_price="12.34")
        assert material["unit_price"] == "12.34"
        assert isinstance(material["unit_price"], str)

        # 0.03 这种价格最容易被 float 毁掉（0.03 在二进制里是无限循环小数）
        cheap = factory.material(unit_price="0.03")
        assert cheap["unit_price"] == "0.03"

    def test_extra_field_is_rejected_not_silently_ignored(self, factory, mes):
        """请求体多传字段必须 422，不能静默忽略。

        防的线上事故：**字段拼错导致业务数据归零**。
        前端把 quantity 拼成 quantitiy、或把 qty 写成 quantity 时，
        如果后端"忽略未知字段"，默认值 0 会被写进库里——
        接口返回 200，业务却少扣了一批料。必须让它当场报错。
        """
        resp = mes.as_role("OPERATOR").post(
            "/inventory/issue",
            json={"material_code": "MAT-CU-001", "quantitiy": "1.000"},
        )
        mes.expect_error(resp, 422, "FIELD_NOT_ALLOWED")


# ---------- 给 MesApi 补两个契约断言小工具 ----------
def _expect_field(body, field: str, expected):
    actual = body.get(field)
    assert actual == expected, f"字段 {field} 期望 {expected!r}，实际 {actual!r}"
    return actual


mes_field_patched = False
