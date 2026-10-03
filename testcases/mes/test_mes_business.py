"""业务场景用例：幂等 / 乐观锁 / 并发 / 权限 / 库存与账实一致。

和 `test_mes_smoke.py` 的分工：
- 冒烟回答"主流程通不通"；
- 这里回答"**出事故的那几条路径**通不通"。

这些场景的共同点是：**单接口用例永远测不出来**。
每一个都对应一种真实事故形态，用例注释里写清了"防的是什么事故"，
因为面试和代码评审时，只讲"我测了这个接口"没有价值，
讲得出"这条用例防的是哪种线上事故"才是价值。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.mes, pytest.mark.api, pytest.mark.regression]

# 领料用的种子物料（在 WH-01 仓，基线 120.000）
SEED_MATERIAL = "MAT-CU-001"
SEED_WAREHOUSE = "WH-01"


@pytest.fixture
def take_material(factory):
    """领料用例的自动退料。

    为什么必须有这个 fixture 而不是"用例自己记得退"：
    领料只 UPDATE stock 那一行，行数完全不变，
    `data_isolation_check` 的行数指标抓不到它，能抓到的只有库存合计指标。
    而"忘了退料"又恰恰是最容易发生的事（尤其断言失败提前 return 时）。
    把退料放进 fixture 的 teardown，用例就算中途红了也会把料退回去 ——
    和 factory.cleanup() 是同一个思路：清理不能依赖用例正文正常走完。
    """

    def _take(*, qty: str, role: str = "OPERATOR", order_no: str | None = None, idem_key: str | None = None):
        # 流水只增不改、没有删除接口，所以必须登记"归属标识"让 factory 直连库回收。
        # 归属标识就是请求头 X-Work-Order-No 的值（不传时为 None），
        # 用例自己随手传的那个字符串才是库里的真实值 —— 按服务端签发的单号是删不到的。
        state["order_no"] = order_no
        factory.register_ledger_owner(order_no)
        return factory.mes.issue(
            role,
            material_code=SEED_MATERIAL,
            qty=qty,
            warehouse=SEED_WAREHOUSE,
            idem_key=idem_key,
            work_order_no=order_no,
        )

    state: dict = {"need_reverse": False}
    try:
        yield _take, state
    finally:
        if state["need_reverse"]:
            resp = factory.mes.as_role("OPERATOR").post(
                "/inventory/reverse",
                json={"material_code": SEED_MATERIAL, "qty": state["qty"], "warehouse": SEED_WAREHOUSE},
                headers={"X-Work-Order-No": state.get("order_no") or ""},
            )
            assert resp.status_code == 200, f"用例收尾退料失败，库存会被永久改动: {resp.text[:300]}"
            # 退料的流水归属是空字符串（teardown 里没有单号可传），
            # 与领料那几条的归属（NULL）不是同一个值，必须一并登记，
            # 否则库存退回去了、流水却留着，会话收尾的隔离检查照样会红。
            factory.register_ledger_owner("")


# ==================== 幂等：防"超时重试变成两笔业务" ====================
class TestIdempotency:
    def test_same_key_same_payload_replays_instead_of_creating_twice(self, factory, mes, unique_tag):
        """同一个幂等键 + 同样的请求体：第二次必须**回放**第一次的响应。

        防的线上事故：**响应在网络上丢了，客户端重试，车间凭空多出一张工单**。
        真实形态是：前端点了"提交"，网关 504 超时，用户又点了一次；
        或者移动端在网络抖动时自动重试。服务端已经落库，客户端却以为失败。
        """
        key = f"idem-replay-{unique_tag}"
        order_no = f"WO-IDEM-{unique_tag}"
        payload = {
            "product_code": "FG-TEST-01",
            "product_name": "幂等回放用例",
            "planned_qty": "10.000",
            "priority": "NORMAL",
            "owner": "张工",
            "order_no": order_no,
        }

        # 第一次就必须带键：幂等的语义是"同一个键的第二次请求被识别为重放"，
        # 首次不带键、重试才带键的话，服务端看到的是两个无关请求。
        first = mes.create_work_order("PLANNER", idem_key=key, **payload)
        first_body = mes.expect_ok(first, 201)
        factory.register_order(first_body["order_no"])

        # 第二次用完全相同的键和请求体（模拟客户端重试）
        second = mes.as_role("PLANNER").post_idem("/work-orders", key, json=payload)

        assert second.status_code == 201, (
            f"重试应拿到与首次一致的成功响应（回放），实际 {second.status_code}: {second.text[:300]}"
        )
        assert second.headers.get("X-Idempotent-Replay") == "true", (
            "重放响应必须带 X-Idempotent-Replay 头，否则运维无法把'重放'和'真新建'区分开，"
            f"排障时会以为系统在重复建单。实际响应头: {dict(second.headers)}"
        )
        assert second.json()["order_no"] == first_body["order_no"], "重放必须返回同一张工单，而不是新建一张"

        # 关键断言：库里只有一张。只看响应码会漏掉"重试其实又建了一张"这种情况。
        listed = mes.expect_ok(mes.as_role("ADMIN").get("/work-orders", params={"product_code": "FG-TEST-01"}))
        same = [w for w in listed["items"] if w["order_no"] == order_no]
        assert len(same) == 1, f"幂等键没能阻止重复建单，库里出现了 {len(same)} 条 {order_no}"

    def test_same_key_different_payload_is_rejected_as_conflict(self, factory, mes, unique_tag):
        """同一个幂等键配不同的请求体：必须 409，而不是"帮你再执行一次"。

        防的线上事故：**客户端生成幂等键的逻辑写错（比如用固定值）**。
        这种情况下如果服务端"宽容"地执行，第二次的请求内容会被静默丢弃，
        客户端以为改成功了，实际数据库里还是第一次的数据 —— 最难查的一类"改了没生效"。
        """
        key = f"idem-conflict-{unique_tag}"
        payload = {
            "product_code": "FG-TEST-01",
            "product_name": "幂等冲突用例",
            "planned_qty": "10.000",
            "priority": "NORMAL",
            "owner": "张工",
            "order_no": f"WO-CONFLICT-{unique_tag}",
        }

        first = mes.as_role("PLANNER").post_idem("/work-orders", key, json=payload)
        mes.expect_ok(first, 201)
        factory.register_order(payload["order_no"])

        # 同一个键，改一个字段（数量从 10 变成 20）
        changed = dict(payload, planned_qty="20.000")
        second = mes.as_role("PLANNER").post_idem("/work-orders", key, json=changed)
        mes.expect_error(second, 409, "IDEMPOTENCY_KEY_CONFLICT")

        # 服务端不能因为"键冲突"就把第一次的数据改掉
        current = mes.expect_ok(mes.work_order(payload["order_no"]))
        mes.expect_field(current, "planned_qty", "10.000")

    def test_idempotency_key_is_released_after_a_failed_request(self, factory, mes, unique_tag):
        """失败的请求必须释放幂等键，否则客户端"改完数据重试"会被永久卡死。

        防的线上事故：**幂等键把接口彻底锁死**。
        典型复盘：第一次请求因为单号重复失败（400），客户端改了个单号重试，
        却收到 409"键已存在" —— 用户看到的是"这个按钮再也点不动了"，
        只能换一个幂等键，而幂等键的语义一旦被这样绕过，等于没做幂等。
        这也是很多人实现幂等时最容易漏的一步。
        """
        key = f"idem-release-{unique_tag}"
        duplicate = f"WO-DUP-{unique_tag}"

        base = {
            "product_code": "FG-TEST-01",
            "product_name": "幂等释放用例",
            "planned_qty": "1.000",
            "priority": "NORMAL",
            "owner": "张工",
        }

        # 先占住这个单号，让第一次请求必然失败（ORDER_NO_DUPLICATE）
        factory.work_order(order_no=duplicate)

        first = mes.create_work_order("PLANNER", **base, order_no=duplicate)
        mes.expect_error(first, 400, "ORDER_NO_DUPLICATE")

        # 改数据后重试：同一个键，必须被允许（说明失败时已把占坑记录删掉）
        fixed = f"WO-FIXED-{unique_tag}"
        second = mes.as_role("PLANNER").post_idem("/work-orders", key, json=dict(base, order_no=fixed))
        second_body = mes.expect_ok(second, 201)
        factory.register_order(second_body["order_no"])

    def test_oversized_idempotency_key_is_rejected_not_truncated(self, factory, mes, unique_tag):
        """超长幂等键必须报错，绝不能截断。

        防的线上事故：**两个不同的请求被截断成同一个键**。
        截断之后，"请求 A 的响应"会被回放给"请求 B"，
        客户端拿到的是别人动作的结果 —— 这比不做幂等更危险，
        因为它会安静地把错误数据交给调用方。
        """
        resp = mes.as_role("PLANNER").post_idem(
            "/work-orders",
            key="k" * 129,
            json={
                "product_code": "FG-TEST-01",
                "product_name": "超长幂等键用例",
                "planned_qty": "1.000",
                "priority": "NORMAL",
                "owner": "张工",
                "order_no": f"WO-LONGKEY-{unique_tag}",
            },
        )
        mes.expect_error(resp, 422, "VALIDATION_ERROR")


# ==================== 乐观锁：防"后写覆盖先写" ====================
class TestOptimisticLock:
    def test_stale_version_is_rejected_with_409(self, factory, mes):
        """拿着过期 version 提交：必须 409 VERSION_CONFLICT。

        防的线上事故：**lost update（后写覆盖先写）**。
        两个计划员同时打开同一张工单，A 先点了"审核通过"；
        B 的页面上还是旧数据，接着点了"取消"。若没有版本校验，
        B 的请求会基于过期状态执行，把 A 刚做的审核无声地覆盖掉。
        这类事故在测试里几乎抓不到，除非专门设计这条用例。
        """
        order_no, _ = factory.work_order()
        stale_version = mes.work_order_version(order_no)

        mes.expect_ok(factory.approve(order_no))

        # 用已经过期的 version 再发一次（模拟 B 手里的旧页面）
        resp = mes.as_role("PLANNER").post(
            f"/work-orders/{order_no}/approve",
            json={"version": stale_version},
        )
        mes.expect_error(resp, 409, "VERSION_CONFLICT")

        # A 的操作结果不能被覆盖：状态还是 APPROVED，version 只前进了一次
        current = mes.expect_ok(mes.work_order(order_no))
        mes.expect_field(current, "status", "APPROVED")
        assert current["version"] == stale_version + 1, (
            f"被拒绝的请求不应改动数据：期望 version 停在 {stale_version + 1}，实际 {current['version']}"
        )

    def test_concurrent_approve_only_one_wins(self, factory, mes, race):
        """两个请求同时审核同一张工单：只能有一个成功，另一个必须 409。

        防的线上事故：**同一张工单被审核两次 / 审核结果互相覆盖**。
        先起跑线对齐再同时发（见 conftest 的 race fixture），
        否则两个请求一前一后跑完，构不成竞态 ——
        用例会"稳定通过"，但其实什么都没验证到。
        """
        order_no, _ = factory.work_order()
        version = mes.work_order_version(order_no)

        def approve():
            return mes.as_role("PLANNER").post(f"/work-orders/{order_no}/approve", json={"version": version})

        responses, elapsed_ms = race([approve, approve])
        codes = sorted(r.status_code for r in responses)

        assert codes == [200, 409], (
            f"并发审核同一条工单，期望恰好一个 200 一个 409，实际 {codes}；"
            f"耗时 {elapsed_ms:.0f}ms；响应: {[r.text[:200] for r in responses]}"
        )
        mes.expect_error(next(r for r in responses if r.status_code == 409), 409, "VERSION_CONFLICT")

        current = mes.expect_ok(mes.work_order(order_no))
        mes.expect_field(current, "status", "APPROVED")
        assert current["version"] == version + 1, "并发下 version 只应前进一次（另一个请求被乐观锁挡掉了）"


# ==================== 并发与库存：防"超卖" ====================
class TestConcurrentInventory:
    def test_concurrent_issue_never_oversells(self, factory, mes, race, take_material):
        """两个请求同时领同样的料：库存合计必须守恒，不能出现负库存。

        防的线上事故：**超卖**。
        经典形态是"先查库存再扣减"之间没有写锁：两个请求都读到 100，
        都判断"够扣 80"，最后库存变成 -60，账上有货、仓里没货。
        Mock 服务用 `BEGIN IMMEDIATE`（见 mock_server/db.py 的 write_tx）
        把第二个请求在 BEGIN 处排队，它读到的一定是最新余额。

        这条用例断言的是**不变量**而不是"必须一个成功一个失败"：
        因为第二个请求可能发现余额已不够而 400，也可能余额充足两个都成功
        —— 两者都正确。真正不能出现的是
        "库存变了，但变的量与成功的领料次数对不上"。

        注意 on_hand_qty 在这里**本来就不该变**（它 = available + reserved + issued，
        领料只是把量从 available 挪到 issued）。所以守恒由两条断言体现：
        issued 的增量 == 30.000 × 成功次数，且 available 的减量与之相等。
        """
        take, state = take_material
        before = mes.stock_of(SEED_MATERIAL, SEED_WAREHOUSE)
        before_available = int(before["available_qty"].replace(".", ""))
        before_issued = int(before["issued_qty"].replace(".", ""))

        per_request = "30.000"
        responses, elapsed_ms = race([lambda: take(qty=per_request), lambda: take(qty=per_request)])

        ok_count = sum(1 for r in responses if r.status_code == 200)
        rejected = [r for r in responses if r.status_code != 200]
        for resp in rejected:
            # 被拒绝的必须是"库存不足"这类明确的业务结论，而不是 500
            assert resp.status_code == 400, f"并发领料被拒时状态码异常: {resp.status_code} {resp.text[:200]}"
            assert mes.error_code(resp) == "INSUFFICIENT_STOCK", resp.text[:200]

        state["need_reverse"] = True
        state["qty"] = f"{30 * ok_count}.000"  # 收尾时把真正领走的量退回去

        after = mes.stock_of(SEED_MATERIAL, SEED_WAREHOUSE)
        delta = 30 * ok_count * 1000
        assert int(after["issued_qty"].replace(".", "")) - before_issued == delta, (
            f"已发料增量与成功领料次数对不上：成功 {ok_count} 次、每次 {per_request}，"
            f"issued 期望 +{delta / 1000:.3f}，实际 {before['issued_qty']} -> {after['issued_qty']}"
            f"（并发耗时 {elapsed_ms:.0f}ms）—— 这就是超卖/漏记的痕迹"
        )
        assert before_available - int(after["available_qty"].replace(".", "")) == delta, (
            f"可用库存减量与已发料增量不一致：{before['available_qty']} -> {after['available_qty']}"
        )
        assert int(after["available_qty"].replace(".", "")) >= 0, "可用库存不能为负 —— 出现了超卖"

        # 流水条数必须与成功的领料次数一致：防"扣了库存却没记流水"这种审计断链
        ledger = mes.expect_ok(
            mes.as_role("ADMIN").get("/inventory/ledger", params={"material_code": SEED_MATERIAL, "txn_type": "ISSUE"})
        )
        assert ledger["total"] >= ok_count, f"成功领料 {ok_count} 次，但流水只有 {ledger['total']} 条"

    def test_issue_beyond_stock_is_rejected_and_stock_unchanged(self, factory, mes, take_material):
        """领料超过可用库存：必须 400 INSUFFICIENT_STOCK，且库存一分不动。

        防的线上事故：**库存被扣成负数**（或者更隐蔽的"扣到 0 就不再扣，
        但业务已经按领走了处理"）。请求被拒绝时数据库必须回到原样，
        不能留下"扣了一半"的中间状态 —— 这就是为什么写操作要包在事务里。
        """
        take, _ = take_material
        before = mes.stock_of(SEED_MATERIAL, SEED_WAREHOUSE)
        available = before["available_qty"]

        over = f"{float(available) + 1:.3f}"
        resp = take(qty=over)
        mes.expect_error(resp, 400, "INSUFFICIENT_STOCK")

        after = mes.stock_of(SEED_MATERIAL, SEED_WAREHOUSE)
        assert after["available_qty"] == available, (
            f"被拒绝的领料改动了库存：请求前 {available}，请求后 {after['available_qty']}"
        )

        # 错误响应必须带上"可用多少、要多少"，否则现场只能猜
        detail = resp.json().get("detail") or {}
        assert detail.get("available") is not None, (
            f"库存不足的错误详情缺少 available，排障时无法定位: {resp.text[:300]}"
        )
        assert detail.get("requested") is not None, (
            f"库存不足的错误详情缺少 requested，排障时无法定位: {resp.text[:300]}"
        )


# ==================== 权限矩阵：防"越权操作" ====================
# (动作描述, HTTP 方法, 路径, 允许的角色集合, 给出的请求体)
# 写成数据表而不是一堆 if：权限矩阵本身就是契约，逐条覆盖一遍最省事，
# 而且以后加接口时，漏掉的组合会以"表里没有"的形式直观暴露出来。
PERMISSION_MATRIX: list[tuple[str, str, str, tuple[str, ...], dict]] = [
    ("新建物料", "POST", "/materials", ("ADMIN", "PLANNER"), {"material_code": "MAT-ACL", "name": "权限用例"}),
    ("删除物料", "DELETE", "/materials/MAT-CU-001", ("ADMIN",), None),
    ("审核工单", "POST", "/work-orders/{order_no}/approve", ("ADMIN", "PLANNER"), {"version": 1}),
    ("发货", "POST", "/work-orders/{order_no}/ship", ("ADMIN",), {"version": 1}),
    ("质检录入", "POST", "/work-orders/{order_no}/inspections", ("ADMIN", "QA"), {"result": "PASS", "version": 1}),
    ("生产领料", "POST", "/inventory/issue", ("ADMIN", "OPERATOR"), {"material_code": "MAT-CU-001", "qty": "1.000"}),
    ("退料", "POST", "/inventory/reverse", ("ADMIN", "OPERATOR"), {"material_code": "MAT-CU-001", "qty": "1.000"}),
]

ALL_ROLES = ("ADMIN", "PLANNER", "OPERATOR", "QA")


def _send(mes, role: str, method: str, path: str, body: dict | None):
    client = mes.as_role(role)
    if method == "POST":
        return client.post(path, json=body or {})
    if method == "DELETE":
        return client.delete(path)
    raise AssertionError(f"权限矩阵里出现了未支持的 HTTP 方法: {method}")


class TestPermissions:
    @pytest.mark.parametrize(
        ("action", "method", "path_template", "allowed", "body"),
        PERMISSION_MATRIX,
        ids=[row[0] for row in PERMISSION_MATRIX],
    )
    def test_permission_matrix(self, factory, mes, action, method, path_template, allowed, body):
        """逐个角色验证"该拒的必须 403，该放的不能 403"。

        防的线上事故：**越权操作**。
        比"操作员删掉了主数据"更常见、更隐蔽的是**漏判**：
        接口忘了调 require()，任何人都能发货。这类缺口在功能测试里
        看起来完全正常（管理员点得动、流程也走得通），只有换角色才暴露。

        注意这里同时断言两个方向：
        - 不在允许集合里的角色 -> 403 PERMISSION_DENIED；
        - 在允许集合里的角色 -> 不能是 403（可以是 400/404/422 等业务结果，
          因为我们并不想把这条用例变成"业务是否成功"的测试）。
        只测前一个方向的用例，会在"权限被收得过紧、正常角色也做不了事"时放行。
        """
        order_no, _ = factory.work_order()
        path = path_template.format(order_no=order_no)

        # 这条用例会**真的建出数据**：允许集合里的角色（这里是 ADMIN/PLANNER）
        # 拿到的是业务成功，不是 403。实测漏登记的后果是共享库多 2 个物料、
        # 1 行库存、2 条领料/退料流水，会话收尾报一堆跟权限毫无关系的差异。
        material_code = (body or {}).get("material_code")

        for role in ALL_ROLES:
            resp = _send(mes, role, method, path, body)
            if role in allowed:
                assert resp.status_code != 403, (
                    f"{action}：{role} 在允许集合 {allowed} 内，却被拒绝；响应: {resp.text[:300]}"
                )
                # 建物料成功时把编码登记回收；只登记"库里真有的"那一次，
                # 重复建会撞 MATERIAL_CODE_DUPLICATE（400），不该进回收列表。
                if method == "POST" and path == "/materials" and material_code and resp.status_code == 201:
                    factory.register_material(material_code)
            else:
                msg = (
                    f"{action}：{role} 不在允许集合 {allowed} 内，却拿到了 {resp.status_code}；响应: {resp.text[:300]}"
                )
                assert resp.status_code == 403, msg
                assert mes.error_code(resp) == "PERMISSION_DENIED", msg

        # 领料/退料的流水没有删除接口，只能直连库按归属标识回收；
        # 权限用例调这两个接口时没传单号，所以库里的归属是 NULL。
        if path in ("/inventory/issue", "/inventory/reverse"):
            factory.register_ledger_owner(None)

    def test_missing_token_is_401_not_403(self, factory, mes):
        """完全不登录：必须是 401，不能是 403。

        防的线上事故：**前端把 401 当成"没权限"处理**。
        401 = 你还没证明身份；403 = 身份没问题但没这个权限。
        混用之后，token 过期时前端不跳登录页，而是弹"您没有权限"，
        用户会去找管理员要权限 —— 一个本该自动刷新的问题被拖成了工单。
        """
        resp = mes.get("/work-orders")
        assert resp.status_code == 401, f"未带令牌应返回 401，实际 {resp.status_code}: {resp.text[:300]}"
        assert mes.error_code(resp) == "UNAUTHENTICATED"

    def test_tampered_token_is_rejected(self, factory, mes):
        """改过签名的令牌必须被拒。

        防的线上事故：**令牌可伪造**（把 role 直接改成 ADMIN）。
        真实系统的令牌是签名过的；这条用例断言签名真的被校验了 ——
        只测"登录成功能拿到令牌"是不够的，那只能证明令牌能签发，
        证明不了它不可伪造。
        """
        token = mes.as_role("OPERATOR").token
        head, _, sig = token.partition(".")
        # 篡改签名（保持格式合法，只是签名对不上）
        tampered = f"{head}.{'A' * len(sig)}"

        resp = mes.get("/work-orders", headers={"Authorization": f"Bearer {tampered}"})
        assert resp.status_code == 401, f"被篡改的令牌被接受了，响应 {resp.status_code}: {resp.text[:200]}"


# ==================== 状态机：防"跳步"和"漏步" ====================
class TestStateMachine:
    def test_approve_an_already_approved_order_is_rejected(self, factory, mes):
        """重复审核：必须 400 ILLEGAL_STATUS_TRANSITION。

        防的线上事故：**同一步业务被做两次**。
        重复审核本身不产生数据错乱，但它会让"审核时间/审核人"被覆盖，
        事后追责时看不到第一次是谁批的。
        """
        order_no, _ = factory.work_order()
        mes.expect_ok(factory.approve(order_no))

        resp = mes.as_role("PLANNER").post(
            f"/work-orders/{order_no}/approve",
            json={"version": mes.work_order_version(order_no)},
        )
        mes.expect_error(resp, 400, "ILLEGAL_STATUS_TRANSITION")

    def test_ship_completed_order_without_stock_in_is_rejected(self, factory, mes):
        """COMPLETED 但从未成品入库就发货：必须被 `_ensure_transition_guards` 拦下。

        防的线上事故：**没入库的货被发出去**。
        状态机只防"跳步"（未审核就排产）是不够的，还要防"漏步"：
        状态已经是 COMPLETED，但成品根本没进过库，此时发货会让账面库存永远对不上。

        【为什么这条用例必须 `force_status`】
        COMPLETED 这个状态只能由 stock_in 服务写出来，所以走接口永远构造不出
        "COMPLETED 且 stocked_in_qty_milli <= 0" 的工单 —— 上一版用例就是这么写的，
        结果请求在更早的 `_ensure_transition` 处（IN_PROGRESS -> SHIPPED 不允许）
        就被挡掉了，detail 里只有 current/target/allowed，根本没有 stock_in。
        也就是说那条断言从来没碰到过它想测的守卫。
        要覆盖"状态对了但前置条件没做"，只能绕过状态机造出这个不可能状态。
        """
        order_no, batch_no = factory.in_production_order(planned_qty="5.000")
        mes.expect_ok(factory.report(order_no, batch_no, good="5.000"))
        mes.expect_ok(factory.inspect(order_no, "PASS", "5.000"), 201)

        factory.force_status(order_no, "COMPLETED")

        resp = mes.as_role("ADMIN").post(
            f"/work-orders/{order_no}/ship",
            json={"version": mes.work_order_version(order_no)},
        )
        mes.expect_error(resp, 400, "ILLEGAL_STATUS_TRANSITION")
        assert "stock_in" in str(resp.json().get("detail")), (
            f"拒单原因应指明缺少的是 stock_in，便于定位: {resp.text[:300]}"
        )

    def test_complete_without_quality_pass_is_rejected(self, factory, mes):
        """质检未通过就完工入库：必须 400 QUALITY_GATE_FAILED。

        防的线上事故：**不良品发给了客户**。
        这是 MES 里代价最高的一类事故：出了厂门再召回，
        成本是当场拦下的几十倍。所以质检结果必须是完工的硬门禁，
        而不是"提醒一下"。
        """
        order_no, batch_no = factory.in_production_order(planned_qty="5.000")
        mes.expect_ok(factory.report(order_no, batch_no, good="5.000"))
        mes.expect_ok(factory.inspect(order_no, "FAIL", "5.000", defect_qty="5.000"), 201)

        resp = factory.stock_in(order_no)
        mes.expect_error(resp, 400, "QUALITY_GATE_FAILED")

    def test_report_beyond_planned_quantity_is_rejected(self, factory, mes):
        """报工数量超过计划数量：必须 400 REPORT_EXCEEDS_PLAN。

        防的线上事故：**产量数据虚高**。
        超额报工往往来自操作工按错数字，或者"把下个班的量提前报上"。
        一旦允许，产量报表、绩效、原料消耗三者会同时错，且互相之间还能"对上"，
        从报表层面完全看不出来。
        """
        order_no, batch_no = factory.in_production_order(planned_qty="5.000")
        resp = factory.report(order_no, batch_no, good="8.000")
        mes.expect_error(resp, 400, "REPORT_EXCEEDS_PLAN")

    def test_batch_quantity_beyond_plan_is_rejected(self, factory, mes):
        """排产批次数量超过工单计划量：必须被拒。

        防的线上事故：**排产量超过客户订单量**。
        多生产出来的部分是净损失（料、工时都花了，客户不会付钱）。
        允许超额排产的直接后果是"车间按批次单领料"，领料量也跟着超。
        """
        order_no, _ = factory.work_order(planned_qty="5.000")
        mes.expect_ok(factory.approve(order_no))
        resp = factory.add_batch(order_no, "8.000")
        mes.expect_error(resp, 400, "REPORT_EXCEEDS_PLAN")
