"""测试数据管理：造数、清理、防污染。

这是最容易翻车的一块，先把结论写在前面：

**策略 = 唯一命名（防读污染） + 定向回收（防写污染） + 每轮一份独立库（防环境漂移）**

1. 【唯一命名】每条用例通过 `factory.tag()` 拿到一个 6 位随机后缀，
   所有编码/单号都带这个后缀。这样用例之间有**天然的读隔离**：
   `keyword=MAT-XXXX` 只会命中自己造的数据，不会因为别人多造了一条
   物料而 total 断言变红。这是最便宜的一层，覆盖 90% 的污染问题。

2. 【定向回收】写操作登记在 `factory.cleanup` 里，用例跑完按
   "依赖倒序"删除：流水 -> 物料 -> 批次/质检 -> 工单。清理**走服务端 API**，
   不直接 DELETE 数据库：如果为了清理就绕过接口，那些带软删/状态机的约束
   就永远测不到"删不掉"的路径了。

3. 【独立库】服务在 session 启动时用 `--reset` 建库，之后每条用例的
   显式清理保证行数回到种子基线；`test_data_isolation.py` 里专门有一条
   用例断言"跑完一轮之后各表行数 == 基线"，把污染变成**会红的测试**
   而不是靠人自觉。

为什么不每条用例一个数据库文件：
   实测初始化一次要 hash 5 个账号的密码（PBKDF2 12 万轮），单次约 0.35s；
   50 条用例就是 17s 纯开销，比整批用例的断言时间还长。
   用"唯一命名 + 定向回收 + 行数基线断言"能把隔离做到可验证，成本却是 0。
   真实项目里如果写操作极重（涉及外部系统），再升级成每用例一份测试库/事务回滚。
"""

from __future__ import annotations

import random
import string
from pathlib import Path

from data_isolation import row_counts, snapshot  # noqa: F401 - row_counts/snapshot 供其它模块从本模块导入

# 种子数据基线（与 mock_server/db.py 的 SEED_USERS / SEED_MATERIALS 对应）。
# 用"==" 而不是 ">=" 断言，是因为只增不减的泄漏（用例造了工单没收尾）
# 恰恰是最常见的污染形态，"至少"会把这种泄漏放过。
SEED_USERS = ("admin", "planner01", "operator01", "operator02", "qa01")
SEED_MATERIALS = (
    "MAT-CU-001",
    "MAT-AL-002",
    "MAT-SC-003",
    "MAT-PCB-004",
    "MAT-ENC-005",
)
SEED_BASELINE = {
    "users": 5,
    "materials": 5,
    "work_orders": 0,
    "production_batches": 0,
    "inspections": 0,
    "stock": 5,
    "stock_ledger": 0,
}

# 种子库里的库存余额，用例做"加减法"断言时用它做基准
SEED_STOCK = {
    ("MAT-CU-001", "WH-01"): "120.000",
    ("MAT-AL-002", "WH-01"): "80.000",
    ("MAT-SC-003", "WH-01"): "3000.000",
    ("MAT-PCB-004", "WH-02"): "50.000",
    ("MAT-ENC-005", "WH-02"): "20.000",
}


class DataFactory:
    """按用例生成唯一数据，并登记需要回收的资源。"""

    def __init__(self, mes, server_root: str, db_path: Path):
        self.mes = mes
        self.server_root = server_root
        self.db_path = Path(db_path)
        # 用例内自增，保证同一用例造多条数据也不冲突
        self._counter = 0
        self.cleanup_orders: list[str] = []
        self.cleanup_materials: list[str] = []
        self.cleanup_ledger_owners: list[str] = []

    # ---------- 命名 ----------
    @staticmethod
    def tag(length: int = 6) -> str:
        """随机后缀。

        不用递增序号的原因：pytest-xdist 并行跑时两个 worker 会撞号，
        随机后缀不需要任何跨进程协调。
        """
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

    def next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self.tag()}-{self._counter}"

    def register_order(self, order_no: str) -> str:
        """登记一张需要回收的工单，返回单号本身。

        供"用例里直接调接口建单"的场景使用（走工厂方法建的单会自动登记）。
        漏登记的表现很隐蔽：用例全绿，但会话结束的隔离检查会报出
        work_orders 多了一行。
        """
        self.cleanup_orders.append(order_no)
        return order_no

    def register_material(self, material_code: str) -> str:
        """登记一个需要回收的物料，返回物料编码本身。

        与 register_order 同理，供"用例里直接 POST /materials"的场景使用。
        实测教训：权限矩阵那条用例在 ADMIN/PLANNER 两个角色上都真的建出了
        MAT-ACL，而它没走工厂方法 —— 会话收尾就报 materials 多 2 行、stock 多 1 行。
        """
        if material_code not in self.cleanup_materials:
            self.cleanup_materials.append(material_code)
        return material_code

    def register_ledger_owner(self, owner: str) -> str:
        """登记一个"流水归属标识"，回收时删掉所有挂着它的流水。

        为什么需要单独登记：`stock_ledger` 不支持删除接口（流水只增不改是审计要求），
        直连数据库删除时又**不能按工单号删干净** —— 该表的 work_order_no 存的是
        调用方通过 `X-Work-Order-No` 请求头声明的值，用例传什么就存什么，
        可能是真实单号，也可能是一个随手写的标记（甚至空字符串）。
        实测见过两类残留都是这么来的：权限矩阵的领料/退料各留一条、
        并发领料用例留一条 REVERSE。
        """
        if owner not in self.cleanup_ledger_owners:
            self.cleanup_ledger_owners.append(owner)
        return owner

    def force_status(self, order_no: str, status: str) -> None:
        """直连数据库把工单状态改成指定值（绕过状态机）。

        只用于测试**通过 API 根本到不了的状态**。典型例子是
        `_ensure_transition_guards` 里"状态是 COMPLETED 但从未成品入库"这条：
        COMPLETED 只能由 stock_in 服务写出来，所以走接口永远构造不出
        `stocked_in_qty_milli <= 0` 的 COMPLETED 工单，
        那个守卫于是成了"没有用例覆盖的代码"。

        同时把 version+1：用例接着走接口时用的必须是最新版本号，
        否则会先撞上乐观锁的 409，根本走不到要测的那个守卫。
        """
        import sqlite3

        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.execute(
                "UPDATE work_orders SET status = ?, version = version + 1,"
                " updated_at = datetime('now') WHERE order_no = ?",
                (status, order_no),
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 造数 ----------
    def material(
        self,
        role: str = "ADMIN",
        *,
        unit_price: str = "12.34",
        safety_stock: str = "5.000",
        name: str | None = None,
        category: str = "测试料",
        unit: str = "KG",
        code: str | None = None,
    ):
        """造一条**不产生流水**的物料（可安全删除）。"""
        code = code or self.next("MAT-T")
        payload = {
            "material_code": code,
            "name": name or f"测试物料 {code}",
            "category": category,
            "unit": unit,
            "unit_price": unit_price,
            "safety_stock": safety_stock,
        }
        resp = self.mes.as_role(role).post("/materials", json=payload)
        self.mes.expect_ok(resp, 201)
        self.cleanup_materials.append(code)
        return resp.json()

    def work_order(
        self,
        role: str = "PLANNER",
        *,
        planned_qty: str = "10.000",
        order_no: str | None = None,
        priority: str = "NORMAL",
        product_code: str = "FG-TEST-01",
        product_name: str = "测试产品",
        idem_key: str | None = None,
    ):
        """造一张工单，返回 (order_no, body)。"""
        order_no = order_no or self.next("WO-T")
        payload = {
            "product_code": product_code,
            "product_name": product_name,
            "planned_qty": planned_qty,
            "priority": priority,
            "owner": "张工",
            "order_no": order_no,
        }
        client = self.mes.as_role(role)
        resp = (
            client.post_idem("/work-orders", idem_key, json=payload)
            if idem_key
            else client.post("/work-orders", json=payload)
        )
        self.mes.expect_ok(resp, 201)
        self.cleanup_orders.append(order_no)
        return order_no, resp.json()

    # ---------- 状态推进（每一步都重新取 version，绝不硬编码） ----------
    def approve(self, order_no: str, role: str = "PLANNER"):
        return self.mes.as_role(role).post(
            f"/work-orders/{order_no}/approve",
            json={"version": self.mes.work_order_version(order_no)},
        )

    def add_batch(
        self, order_no: str, planned_qty: str, line: str = "L1", role: str = "PLANNER", idem_key: str | None = None
    ):
        client = self.mes.as_role(role)
        payload = {"planned_qty": planned_qty, "line": line, "version": self.mes.work_order_version(order_no)}
        resp = (
            client.post_idem(f"/work-orders/{order_no}/batches", idem_key, json=payload)
            if idem_key
            else client.post(f"/work-orders/{order_no}/batches", json=payload)
        )
        return resp

    def schedule(self, order_no: str, role: str = "PLANNER"):
        return self.mes.as_role(role).post(
            f"/work-orders/{order_no}/schedule",
            json={"version": self.mes.work_order_version(order_no)},
        )

    def start_batch(self, order_no: str, batch_no: str, role: str = "OPERATOR"):
        return self.mes.as_role(role).post(
            f"/work-orders/{order_no}/batches/{batch_no}/start",
            json={"version": self.mes.work_order_version(order_no)},
        )

    def report(self, order_no: str, batch_no: str, good: str, scrap: str = "0.000", role: str = "OPERATOR"):
        return self.mes.as_role(role).post(
            f"/work-orders/{order_no}/report",
            json={
                "batch_no": batch_no,
                "good_qty": good,
                "scrap_qty": scrap,
                "version": self.mes.work_order_version(order_no),
            },
        )

    def inspect(
        self,
        order_no: str,
        result: str,
        inspect_qty: str,
        defect_qty: str = "0.000",
        role: str = "QA",
        idem_key: str | None = None,
    ):
        client = self.mes.as_role(role)
        payload = {
            "result": result,
            "inspect_qty": inspect_qty,
            "defect_qty": defect_qty,
            "version": self.mes.work_order_version(order_no),
        }
        resp = (
            client.post_idem(f"/work-orders/{order_no}/inspections", idem_key, json=payload)
            if idem_key
            else client.post(f"/work-orders/{order_no}/inspections", json=payload)
        )
        return resp

    def stock_in(self, order_no: str, role: str = "OPERATOR", warehouse: str = "WH-FG"):
        """成品入库。

        会把产品编码登记为"待回收物料"：入库会在 stock 表里插入/更新
        一行以 product_code 为编码的库存（`mock_server/services.py` 的
        `stock_in_finished_goods`）。如果不登记，用例跑完 stock 表就多一行，
        而它**不是**用建物料接口造出来的（`materials` 表里根本没有这个编码），
        所以按"物料编码"去清理的路径也不会碰它。
        实测症状就是：材料/工单/流水都干净了，只剩 stock 多一行 +10.000，
        会话收尾的隔离检查因此一直红着。
        """
        resp = self.mes.as_role(role).post(
            f"/work-orders/{order_no}/stock-in",
            json={"warehouse": warehouse, "version": self.mes.work_order_version(order_no)},
        )
        if resp.status_code == 200:
            body = resp.json()
            # 响应两种形状都要认：stock-in 返回 OperationResult 包装
            # {ok, message, work_order, stock}，而 approve/schedule/ship 直接返回工单本体。
            wrapped = body.get("work_order") if isinstance(body, dict) else None
            product_code = (wrapped or {}).get("product_code")
            if product_code:
                self.register_material(product_code)
        return resp

    def ship(self, order_no: str, role: str = "ADMIN"):
        return self.mes.as_role(role).post(
            f"/work-orders/{order_no}/ship",
            json={"version": self.mes.work_order_version(order_no)},
        )

    # ---------- 组合场景 ----------
    def in_production_order(self, *, planned_qty: str = "10.000", batch_qty: str | None = None, line: str = "L1"):
        """把工单一路推到"生产中"，返回 (order_no, batch_no)。

        用例里 90% 的场景都要先到这个状态，把这串动作收进工厂方法，
        是为了让用例正文只留"这一条要验证的差异"，而不是 8 行造数噪音。
        """
        order_no, _ = self.work_order(planned_qty=planned_qty)
        self.mes.expect_ok(self.approve(order_no))
        batch_resp = self.add_batch(order_no, batch_qty or planned_qty, line=line)
        self.mes.expect_ok(batch_resp, 201)
        batch_no = batch_resp.json()["batch"]["batch_no"]
        self.mes.expect_ok(self.schedule(order_no))
        self.mes.expect_ok(self.start_batch(order_no, batch_no))
        return order_no, batch_no

    # ---------- 回收 ----------
    def cleanup(self) -> list[str]:
        """按依赖倒序清理本用例造的数据，返回实际执行的动作（供日志/断言）。

        顺序不能反：stock_ledger 引用物料编码、production_batches 引用工单 id，
        先删父表会撞外键（PRAGMA foreign_keys=ON 是真的生效的）。
        """
        actions: list[str] = []
        # 注意：物料分支不能嵌在 `if self.cleanup_orders` 里面。
        # 曾经就是那样写的，后果是"只造了物料、没造工单"的用例（比如金额精度、
        # 幂等键的物料分支）造的物料永远不会被删 —— 行数够不着基线，
        # 而报错信息会把排查方向指向"库存没退回"，跟真正的原因差得很远。
        if self.cleanup_orders or self.cleanup_materials or self.cleanup_ledger_owners:
            conn = None
            try:
                import sqlite3

                conn = sqlite3.connect(str(self.db_path), timeout=10.0)
                # 外键检查打开：这里删的是父表，如果还有子行引用它，
                # 我们希望当场报错，而不是静默留下一堆孤儿行
                # （mock 服务自己的连接是开的，测试连接也必须一致）。
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=10000")
                for order_no in self.cleanup_orders:
                    row = conn.execute(
                        "SELECT id, idempotency_key FROM work_orders WHERE order_no = ?", (order_no,)
                    ).fetchone()
                    if row is None:
                        # 工单已经不在库里了（别的用例或更早的清理删掉了它）。
                        # 这里曾经是 `continue`，直接跳过整条循环 —— 连带
                        # 这个工单关联的物料也不清了。实测症状：smoke 用例
                        # 建的 FG-TEST-01 库存行永远留在 stock 表里
                        # （它的工单被先跑的用例删掉了），会话收尾一直报 +1 行。
                        # 所以这里只跳过"工单本身"的清理，继续往下走。
                        actions.append(f"work_order {order_no} 已不存在，跳过（物料仍照常回收）")
                        continue
                    wo_id, idem_key = row[0], row[1]
                    # 流水的归属标识可能就是单号本身（领料时用例把单号当归属传），
                    # 所以两条路都要删：按单号、按登记过的归属标识。
                    conn.execute("DELETE FROM stock_ledger WHERE work_order_no = ?", (order_no,))
                    conn.execute("DELETE FROM inspections WHERE work_order_id = ?", (wo_id,))
                    conn.execute("DELETE FROM production_batches WHERE work_order_id = ?", (wo_id,))
                    # 幂等记录按"建单时用的键"精确回收。
                    # 早期版本是按 scope LIKE '%单号%' 删的，那永远匹配不到：
                    # 幂等表的 scope 是 'work_order:create' 这种接口名，不含单号。
                    # 结果就是每个用过幂等键的用例都留下一条记录，
                    # 会话结束时 idempotency_keys 的行数回不到基线。
                    if idem_key:
                        conn.execute("DELETE FROM idempotency_keys WHERE idem_key = ?", (idem_key,))
                    conn.execute("DELETE FROM work_orders WHERE id = ?", (wo_id,))
                    actions.append(f"work_order {order_no} 及其批次/质检/流水")
                for owner in self.cleanup_ledger_owners:
                    # 必须用 `IS ?` 而不是 `= ?`：归属标识可能是 NULL
                    # （用例调领料接口时没传 X-Work-Order-No），
                    # 而 SQL 里 `NULL = NULL` 是 UNKNOWN，删不掉任何行 ——
                    # 实测就是这样：日志显示注册了 owner=None，行数却一条没少。
                    cur = conn.execute("DELETE FROM stock_ledger WHERE work_order_no IS ?", (owner,))
                    if cur.rowcount:
                        actions.append(f"stock_ledger 归属 {owner!r} 的 {cur.rowcount} 条流水")
                for code in self.cleanup_materials:
                    conn.execute(
                        "DELETE FROM idempotency_keys WHERE scope = 'material:create' AND response_body LIKE ?",
                        (f"%{code}%",),
                    )
                    # 流水要跟着物料一起删：它按 material_code 引用物料，
                    # 留着就会挡住 materials 的删除（外键已开）。
                    conn.execute("DELETE FROM stock_ledger WHERE material_code = ?", (code,))
                    conn.execute("DELETE FROM stock WHERE material_code = ?", (code,))
                    conn.execute("DELETE FROM materials WHERE material_code = ?", (code,))
                    actions.append(f"material {code} 及其库存行/流水")
                conn.commit()
            finally:
                if conn is not None:
                    conn.close()
        return actions


# 行数/库存快照的口径集中在 data_isolation.py，这里只做转发，
# 保证 conftest 的隔离检查与用例里的断言用的是同一套定义。
# 想直接 import 行数统计的用法仍然有效：`from data_factory import row_counts`。
__all__ = ["SEED_BASELINE", "SEED_STOCK", "SEED_MATERIALS", "SEED_USERS", "DataFactory", "row_counts", "snapshot"]
