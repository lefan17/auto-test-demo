"""工单与生产业务规则（服务层）。

这一层是"业务复杂度"的集中地，也是用例真正能抓到缺陷的地方：
状态机、乐观锁、数量精度、报工上限、质检门禁、完工数量一致性。

设计上刻意把规则都写成**显式的表**而不是散落的 if：

    ALLOWED_TRANSITIONS = {"PENDING": ("APPROVED", "CANCELLED"), ...}

好处有两个（面试可以直接讲）：
1. 新增状态时只改这张表，不用在十几个 if 里找漏；
2. 用例可以**参数化遍历这张表**做"非法跃迁全覆盖"，
   而不是人工挑两三种组合——这是"用例可枚举"和"用例靠经验"的区别。
"""

from __future__ import annotations

import os
import time

from mock_server.db import rows_to_dicts
from mock_server.errors import (
    BatchCodeInUse,
    BatchNotInProduction,
    IllegalTransition,
    InsufficientStock,
    MaterialCodeDuplicate,
    MaterialInUse,
    NotFound,
    OrderNoDuplicate,
    PriorityInvalid,
    QualityGateFailed,
    QuantityMismatch,
    ReportExceedsPlan,
    StockNotEnoughForReverse,
    ValidationFailed,
    VersionConflict,
)
from mock_server.models import (
    batch_out,
    inspection_out,
    ledger_out,
    material_out,
    stock_out,
    work_order_out,
)
from mock_server.quantity import cent_to_str, parse_cent, parse_qty, parse_qty_allow_zero
from mock_server.util import next_batch_no, next_order_no, now_iso

# ==================== 状态机定义 ====================
# 待审核 -> 已审核 -> 已排产 -> 生产中 -> 已完工 -> 已发货
# 取消只允许在"还没有产生实物出入库"的阶段发生。
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PENDING": ("APPROVED", "CANCELLED"),
    "APPROVED": ("SCHEDULED", "CANCELLED"),
    "SCHEDULED": ("IN_PROGRESS", "CANCELLED"),
    "IN_PROGRESS": ("COMPLETED", "CANCELLED"),
    # 已完工之后不允许取消：成品已经入库，库存和财务凭证都生成了，
    # 这时要冲销必须走"退货/红冲"流程，不能让状态机随手改回去。
    "COMPLETED": ("SHIPPED",),
    "SHIPPED": (),
    "CANCELLED": (),
}

# 状态变更需要的前置条件（文档化用，实际校验在 _ensure_transition_guards 里）
TRANSITION_GUARDS: dict[str, str] = {
    "APPROVED": "必须由 PLANNER/ADMIN 审批",
    "SCHEDULED": "必须先创建至少一个生产批次",
    "IN_PROGRESS": "至少一个批次已开工",
    "COMPLETED": "必须已报工，且质检结果为 PASS 或 WAIVED",
    "SHIPPED": "必须已完成成品入库",
}

# 排序字段白名单：外部字段名 -> 数据库列名
WORK_ORDER_SORTABLE = {
    "id": "id",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "planned_qty": "planned_qty_milli",
    "priority": "priority",
    "status": "status",
    "order_no": "order_no",
}

MATERIAL_SORTABLE = {
    "id": "id",
    "material_code": "material_code",
    "name": "name",
    "unit_price": "unit_price_cent",
    "safety_stock": "safety_stock_milli",
    "created_at": "created_at",
}

PRIORITIES = ("LOW", "NORMAL", "HIGH", "URGENT")
NON_CANCELLABLE_AFTER = ("IN_PROGRESS",)

# 并发可复现性：在"检查库存/检查版本"和"写库"之间插入一个短暂的等待窗口。
# 真实系统的窗口来自网络和 GC，长短随机、有时根本复现不出来；
# Mock 服务把窗口固定住，才能让并发用例**每次都红/每次都绿**，
# 而不是变成"偶尔失败、被大家忽略"的 flaky 用例。
CONCURRENCY_WINDOW_MS = int(os.getenv("MOCK_CONCURRENCY_WINDOW_MS", "120"))


def concurrency_window() -> None:
    window = CONCURRENCY_WINDOW_MS
    if window > 0:
        time.sleep(window / 1000.0)


def _check_version(entity: str, current: int, provided: int) -> None:
    """乐观锁校验。

    防的事故：两个计划员同时打开同一个工单，A 先改成"已审核"，
    B 页面上还是旧数据，接着点了"取消"——如果没有版本校验，
    B 的请求会基于过期状态执行，把 A 刚做的审核覆盖掉。
    这类"后写覆盖先写"（lost update）在测试里几乎抓不到，除非专门设计并发用例。
    """
    if provided != current:
        raise VersionConflict(
            f"{entity} version 不匹配：客户端持有 {provided}，服务端为 {current}；请重新拉取最新数据后重试",
            entity=entity,
            client_version=provided,
            server_version=current,
        )


def _ensure_transition(current: str, target: str):
    allowed = ALLOWED_TRANSITIONS.get(current, ())
    if target not in allowed:
        raise IllegalTransition(
            f"不允许从 {current} 变更为 {target}",
            current=current,
            target=target,
            allowed=list(allowed),
        )


def _ensure_transition_guards(conn, wo, target: str) -> None:
    """状态变更的业务前置条件。这些是"状态对了但事情没做完"的情况。"""
    if target == "SCHEDULED":
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM production_batches WHERE work_order_id = ?",
            (wo["id"],),
        ).fetchone()["c"]
        if count == 0:
            raise IllegalTransition("工单没有生产批次，不能排产", missing="production_batches")

    if target == "IN_PROGRESS":
        running = conn.execute(
            "SELECT COUNT(*) AS c FROM production_batches WHERE work_order_id = ? AND status = 'IN_PROGRESS'",
            (wo["id"],),
        ).fetchone()["c"]
        if running == 0:
            raise IllegalTransition("没有已开工的批次，不能进入生产中", missing="running_batch")

    if target == "COMPLETED":
        if wo["reported_good_milli"] + wo["reported_scrap_milli"] <= 0:
            raise IllegalTransition("尚未报工，不能完工", missing="production_report")
        if wo["qc_result"] not in ("PASS", "WAIVED"):
            raise QualityGateFailed(
                f"质检结果为 {wo['qc_result']}，不允许完工",
                qc_result=wo["qc_result"],
            )

    if target == "SHIPPED" and wo["stocked_in_qty_milli"] <= 0:
        raise IllegalTransition("尚未成品入库，不能发货", missing="stock_in")


def _bump(conn, wo_id: int, now: str) -> None:
    """状态变更统一入口：version 自增 + updated_at 刷新。

    所有变更都必须走这里，"忘记加版本号"这种事就不靠人记了。
    """
    conn.execute(
        "UPDATE work_orders SET version = version + 1, updated_at = ? WHERE id = ?",
        (now, wo_id),
    )


def list_batches(conn, order_no: str) -> list[dict]:
    """工单下的批次列表。用例校验状态流转结果时要靠它取证。"""
    wo = get_work_order_row(conn, order_no)
    rows = conn.execute(
        "SELECT * FROM production_batches WHERE work_order_id = ? ORDER BY id ASC",
        (wo["id"],),
    ).fetchall()
    return [batch_out(r) for r in rows]


def list_inspections(conn, order_no: str) -> list[dict]:
    wo = get_work_order_row(conn, order_no)
    rows = conn.execute(
        "SELECT * FROM inspections WHERE work_order_id = ? ORDER BY id ASC",
        (wo["id"],),
    ).fetchall()
    return [inspection_out(r) for r in rows]


def get_work_order_row(conn, order_no: str):
    row = conn.execute("SELECT * FROM work_orders WHERE order_no = ?", (order_no,)).fetchone()
    if row is None:
        raise NotFound(f"工单不存在: {order_no}", order_no=order_no)
    return row


def work_order_row_out(conn, order_no: str) -> dict:
    """给只读接口用的便捷函数：查行并序列化。"""
    return work_order_out(get_work_order_row(conn, order_no))


def list_work_orders(
    conn,
    *,
    status=None,
    priority=None,
    product_code=None,
    owner=None,
    include_cancelled: bool = True,
    sort_column="id",
    direction="ASC",
    offset=0,
    limit=20,
):
    where, params = ["1=1"], []
    if not include_cancelled:
        where.append("status <> 'CANCELLED'")
    if status:
        where.append("status = ?")
        params.append(status)
    if priority:
        if priority not in PRIORITIES:
            raise PriorityInvalid(f"priority 取值非法: {priority}", allowed=list(PRIORITIES))
        where.append("priority = ?")
        params.append(priority)
    if product_code:
        where.append("product_code = ?")
        params.append(product_code)
    if owner:
        where.append("owner = ?")
        params.append(owner)

    clause = " AND ".join(where)
    # sort_column 只能来自白名单（deps.resolve_sort 产出），不是用户原样输入
    total = conn.execute(f"SELECT COUNT(*) AS c FROM work_orders WHERE {clause}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM work_orders WHERE {clause} ORDER BY {sort_column} {direction}, id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [work_order_out(r) for r in rows], total


def create_work_order(conn, *, current_user, payload, idem_key: str | None):
    if payload.priority not in PRIORITIES:
        raise PriorityInvalid(f"priority 取值非法: {payload.priority}", allowed=list(PRIORITIES))
    planned = parse_qty(payload.planned_qty, "planned_qty")
    order_no = payload.order_no or next_order_no()

    if conn.execute("SELECT 1 FROM work_orders WHERE order_no = ?", (order_no,)).fetchone():
        # 单号唯一：真实系统里单号可能是人工输入或离线排产带进来的，
        # 重复单号会导致后续所有出入库都记到错的工单上。
        raise OrderNoDuplicate(f"工单号已存在: {order_no}", order_no=order_no)

    now = now_iso()
    cur = conn.execute(
        "INSERT INTO work_orders (order_no, product_code, product_name, planned_qty_milli,"
        " status, priority, owner, idempotency_key, created_at, updated_at, version)"
        " VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, 1)",
        (
            order_no,
            payload.product_code,
            payload.product_name,
            planned,
            payload.priority,
            payload.owner,
            idem_key,
            now,
            now,
        ),
    )
    return work_order_out(get_work_order_row(conn, order_no)), wo_id_of(cur)


def wo_id_of(cursor) -> int:
    return int(cursor.lastrowid)


def approve_work_order(conn, order_no: str, version: int, current_user):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], version)
    _ensure_transition(wo["status"], "APPROVED")
    _ensure_transition_guards(conn, wo, "APPROVED")
    now = now_iso()
    conn.execute(
        "UPDATE work_orders SET status='APPROVED', reviewer=?, updated_at=? WHERE id=?",
        (current_user.username, now, wo["id"]),
    )
    _bump(conn, wo["id"], now)
    return work_order_out(get_work_order_row(conn, order_no))


def add_batch(conn, order_no: str, payload, current_user):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], payload.version)
    if wo["status"] != "APPROVED":
        raise IllegalTransition(
            f"只有已审核的工单才能排产，当前 {wo['status']}",
            current=wo["status"],
            required="APPROVED",
        )

    planned = parse_qty(payload.planned_qty, "planned_qty")
    already = conn.execute(
        "SELECT COALESCE(SUM(planned_qty_milli), 0) AS s FROM production_batches WHERE work_order_id = ?",
        (wo["id"],),
    ).fetchone()["s"]
    if already + planned > wo["planned_qty_milli"]:
        raise ReportExceedsPlan(
            "批次计划数量合计超过工单计划数量",
            order_planned=wo["planned_qty_milli"],
            batch_sum=already + planned,
        )

    batch_no = payload.batch_no or next_batch_no()
    if conn.execute("SELECT 1 FROM production_batches WHERE batch_no = ?", (batch_no,)).fetchone():
        raise BatchCodeInUse(f"批次号已存在: {batch_no}", batch_no=batch_no)

    now = now_iso()
    cur = conn.execute(
        "INSERT INTO production_batches (batch_no, work_order_id, planned_qty_milli,"
        " line, operator, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (batch_no, wo["id"], planned, payload.line, payload.operator or current_user.username, now, now),
    )
    batch_id = int(cur.lastrowid)
    _bump(conn, wo["id"], now)
    row = conn.execute("SELECT * FROM production_batches WHERE id = ?", (batch_id,)).fetchone()
    return batch_out(row), wo_id_of(cur)


def start_batch(conn, order_no: str, batch_no: str, version: int):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], version)
    batch = conn.execute(
        "SELECT * FROM production_batches WHERE batch_no = ? AND work_order_id = ?",
        (batch_no, wo["id"]),
    ).fetchone()
    if batch is None:
        raise NotFound(f"批次不存在: {batch_no}", batch_no=batch_no, order_no=order_no)
    if wo["status"] != "SCHEDULED":
        raise IllegalTransition(
            f"批次开工要求工单处于已排产，当前 {wo['status']}",
            current=wo["status"],
            required="SCHEDULED",
        )
    if batch["status"] != "CREATED":
        raise IllegalTransition(
            f"批次 {batch_no} 当前 {batch['status']}，不能重复开工",
            current=batch["status"],
        )

    now = now_iso()
    conn.execute(
        "UPDATE production_batches SET status='IN_PROGRESS', version=version+1, updated_at=? WHERE id=?",
        (now, batch["id"]),
    )
    # 批次一开工，工单自动进入生产中（真实 MES 的常见联动）
    conn.execute(
        "UPDATE work_orders SET status='IN_PROGRESS', updated_at=? WHERE id=?",
        (now, wo["id"]),
    )
    _bump(conn, wo["id"], now)
    return work_order_out(get_work_order_row(conn, order_no)), batch_out(
        conn.execute("SELECT * FROM production_batches WHERE id=?", (batch["id"],)).fetchone()
    )


def transition_work_order(conn, order_no: str, target: str, version: int, current_user):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], version)
    _ensure_transition(wo["status"], target)
    _ensure_transition_guards(conn, wo, target)

    now = now_iso()
    conn.execute(
        "UPDATE work_orders SET status = ?, updated_at = ? WHERE id = ?",
        (target, now, wo["id"]),
    )
    _bump(conn, wo["id"], now)
    return work_order_out(get_work_order_row(conn, order_no))


def report_production(conn, order_no: str, payload, current_user):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], payload.version)

    batch = conn.execute(
        "SELECT * FROM production_batches WHERE batch_no = ? AND work_order_id = ?",
        (payload.batch_no, wo["id"]),
    ).fetchone()
    if batch is None:
        raise NotFound(f"批次不存在: {payload.batch_no}", batch_no=payload.batch_no)
    if batch["status"] != "IN_PROGRESS":
        raise BatchNotInProduction(
            f"批次 {payload.batch_no} 状态为 {batch['status']}，只有生产中才能报工",
            batch_status=batch["status"],
        )

    good = parse_qty_allow_zero(payload.good_qty, "good_qty")
    scrap = parse_qty_allow_zero(payload.scrap_qty, "scrap_qty")
    if good + scrap <= 0:
        raise ValidationFailed("good_qty 与 scrap_qty 不能同时为 0")

    planned = wo["planned_qty_milli"]
    reported = wo["reported_good_milli"] + wo["reported_scrap_milli"]
    if reported + good + scrap > planned:
        # 报工不能超计划。防的事故：超报会把良率算成 >100%，
        # 后面按良率做的成本分摊、绩效考核全部失真。
        raise ReportExceedsPlan(
            f"累计报工 {reported + good + scrap} 超过计划 {planned}",
            planned=planned,
            attempting=reported + good + scrap,
            remaining=max(planned - reported, 0),
        )

    now = now_iso()
    conn.execute(
        "UPDATE production_batches SET good_qty_milli = good_qty_milli + ?,"
        " scrap_qty_milli = scrap_qty_milli + ?, updated_at = ? WHERE id = ?",
        (good, scrap, now, batch["id"]),
    )
    conn.execute(
        "UPDATE work_orders SET reported_good_milli = reported_good_milli + ?,"
        " reported_scrap_milli = reported_scrap_milli + ?, updated_at = ? WHERE id = ?",
        (good, scrap, now, wo["id"]),
    )
    _bump(conn, wo["id"], now)
    return work_order_out(get_work_order_row(conn, order_no))


def create_inspection(conn, order_no: str, payload, current_user):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], payload.version)

    result = (payload.result or "").upper()
    if result not in ("PASS", "FAIL", "WAIVED"):
        raise ValidationFailed(f"result 只能取 PASS/FAIL/WAIVED，实际 {payload.result!r}")
    if wo["status"] not in ("IN_PROGRESS", "COMPLETED"):
        raise IllegalTransition(
            f"工单状态 {wo['status']} 不允许质检",
            current=wo["status"],
            allowed=["IN_PROGRESS", "COMPLETED"],
        )

    inspect_qty = parse_qty(payload.inspect_qty, "inspect_qty")
    defect_qty = parse_qty_allow_zero(payload.defect_qty, "defect_qty")
    if defect_qty > inspect_qty:
        raise ValidationFailed(f"defect_qty({defect_qty}) 不能大于 inspect_qty({inspect_qty})")

    now = now_iso()
    cur = conn.execute(
        "INSERT INTO inspections (work_order_id, result, inspect_qty_milli,"
        " defect_qty_milli, note, inspector, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (wo["id"], result, inspect_qty, defect_qty, payload.note, current_user.username, now),
    )
    conn.execute(
        "UPDATE work_orders SET qc_result=?, qc_note=?, updated_at=? WHERE id=?",
        (result, payload.note, now, wo["id"]),
    )
    _bump(conn, wo["id"], now)
    row = conn.execute("SELECT * FROM inspections WHERE id = ?", (cur.lastrowid,)).fetchone()
    return work_order_out(get_work_order_row(conn, order_no)), inspection_out(row)


def stock_in_finished_goods(conn, order_no: str, payload, current_user):
    wo = get_work_order_row(conn, order_no)
    _check_version("工单", wo["version"], payload.version)

    if wo["status"] != "IN_PROGRESS":
        raise IllegalTransition(f"只有生产中的工单可以入库，当前 {wo['status']}", current=wo["status"])
    if wo["qc_result"] not in ("PASS", "WAIVED"):
        # 质检门禁：没质检或质检不合格就入库，等于把不良品发给客户。
        raise QualityGateFailed(
            f"质检结果为 {wo['qc_result']}，不允许入库",
            qc_result=wo["qc_result"],
        )

    good = wo["reported_good_milli"]
    scrap = wo["reported_scrap_milli"]
    if good <= 0:
        raise IllegalTransition("没有良品可入库", good_qty_milli=good)
    if good != wo["planned_qty_milli"] or scrap > 0:
        # 良品数 != 计划数 或 有报废：真实工厂要求走"超领/补料/让步接收"审批，
        # 不能直接按实际数入库（否则计划达成率统计就没有口径了）。
        raise QuantityMismatch(
            f"良品 {good} / 计划 {wo['planned_qty_milli']} / 报废 {scrap}，不满足入库条件（需良品=计划且无报废）",
            good_qty=good,
            planned_qty=wo["planned_qty_milli"],
            scrap_qty=scrap,
        )

    warehouse = payload.warehouse
    now = now_iso()
    row = conn.execute(
        "SELECT * FROM stock WHERE material_code = ? AND warehouse = ?",
        (wo["product_code"], warehouse),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO stock (material_code, warehouse, available_milli, updated_at) VALUES (?, ?, 0, ?)",
            (wo["product_code"], warehouse, now),
        )
        row = conn.execute(
            "SELECT * FROM stock WHERE material_code = ? AND warehouse = ?",
            (wo["product_code"], warehouse),
        ).fetchone()

    new_available = row["available_milli"] + good
    conn.execute(
        "UPDATE stock SET available_milli = ?, updated_at = ? WHERE id = ?",
        (new_available, now, row["id"]),
    )
    _set_ledger_operator(
        conn,
        "RECEIPT",
        wo["product_code"],
        warehouse,
        good,
        new_available,
        wo["order_no"],
        None,
        current_user.username,
        payload.remark or "成品入库",
    )

    conn.execute(
        "UPDATE work_orders SET status='COMPLETED', stocked_in_qty_milli = ?, updated_at = ? WHERE id = ?",
        (good, now, wo["id"]),
    )
    _bump(conn, wo["id"], now)
    return work_order_out(get_work_order_row(conn, order_no)), stock_out(
        conn.execute("SELECT * FROM stock WHERE id=?", (row["id"],)).fetchone()
    )


# ==================== 库存 ====================
def _set_ledger_operator(
    conn,
    txn_type: str,
    material_code: str,
    warehouse: str,
    qty: int,
    balance: int,
    order_no,
    batch_no: str | None,
    operator: str,
    remark: str | None,
) -> None:
    """写一条出入库流水。

    流水的字段由**服务层显式提供**，不靠数据库触发器，原因是触发器拿不到
    "谁操作的、关联哪个批次"这些上下文。审计日志缺了操作人，
    出事时根本查不出是谁干的——这比没有日志更糟。
    """
    conn.execute(
        "INSERT INTO stock_ledger (material_code, warehouse, txn_type, qty_milli,"
        " balance_milli, work_order_no, batch_no, operator, remark, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (material_code, warehouse, txn_type, qty, balance, order_no, batch_no, operator, remark, now_iso()),
    )


def issue_material(conn, payload, current_user, *, order_no: str | None = None):
    """生产领料：可用库存减少、已发料增加。"""
    qty = parse_qty(payload.qty, "qty")
    material = conn.execute(
        "SELECT * FROM materials WHERE material_code = ? AND deleted_at IS NULL",
        (payload.material_code,),
    ).fetchone()
    if material is None:
        raise NotFound(f"物料不存在或已删除: {payload.material_code}", material_code=payload.material_code)

    row = conn.execute(
        "SELECT * FROM stock WHERE material_code = ? AND warehouse = ?",
        (payload.material_code, payload.warehouse),
    ).fetchone()
    if row is None:
        raise NotFound(
            f"仓库 {payload.warehouse} 没有物料 {payload.material_code} 的库存记录",
            material_code=payload.material_code,
            warehouse=payload.warehouse,
        )

    # 这里是"超卖"的经典位置：先查库存再扣减。
    # 窗口放在这个 sleep 里，并发的第二个请求会拿着同样(过期)的读数继续往下走，
    # 只有靠 BEGIN IMMEDIATE 才能保证它读到的是最新值。
    concurrency_window()

    if row["available_milli"] < qty:
        raise InsufficientStock(
            f"物料 {payload.material_code} 可用 {row['available_milli']}，本次领用 {qty}",
            available=row["available_milli"],
            requested=qty,
            material_code=payload.material_code,
        )

    new_available = row["available_milli"] - qty
    new_issued = row["issued_milli"] + qty
    now = now_iso()
    conn.execute(
        "UPDATE stock SET available_milli = ?, issued_milli = ?, updated_at = ? WHERE id = ?",
        (new_available, new_issued, now, row["id"]),
    )
    _set_ledger_operator(
        conn,
        "ISSUE",
        payload.material_code,
        payload.warehouse,
        qty,
        new_available,
        order_no,
        payload.batch_no,
        current_user.username,
        payload.remark or "生产领料",
    )
    return stock_out(conn.execute("SELECT * FROM stock WHERE id=?", (row["id"],)).fetchone())


def reverse_material(conn, payload, current_user, *, order_no: str | None = None):
    """退料：已发料减少、可用库存回补。退料量不能超过已发量。"""
    qty = parse_qty(payload.qty, "qty")
    row = conn.execute(
        "SELECT * FROM stock WHERE material_code = ? AND warehouse = ?",
        (payload.material_code, payload.warehouse),
    ).fetchone()
    if row is None:
        raise NotFound(f"仓库 {payload.warehouse} 没有物料 {payload.material_code} 的库存记录")
    if row["issued_milli"] < qty:
        raise StockNotEnoughForReverse(
            f"物料 {payload.material_code} 已发料 {row['issued_milli']}，退料 {qty} 超过已发数量",
            issued=row["issued_milli"],
            requested=qty,
        )

    new_available = row["available_milli"] + qty
    new_issued = row["issued_milli"] - qty
    now = now_iso()
    conn.execute(
        "UPDATE stock SET available_milli = ?, issued_milli = ?, updated_at = ? WHERE id = ?",
        (new_available, new_issued, now, row["id"]),
    )
    _set_ledger_operator(
        conn,
        "REVERSE",
        payload.material_code,
        payload.warehouse,
        qty,
        new_available,
        order_no,
        payload.batch_no,
        current_user.username,
        payload.remark or "生产退料",
    )
    return stock_out(conn.execute("SELECT * FROM stock WHERE id=?", (row["id"],)).fetchone())


def list_stock(
    conn, *, material_code=None, warehouse=None, sort_column="material_code", direction="ASC", offset=0, limit=20
):
    where, params = ["1=1"], []
    if material_code:
        where.append("material_code = ?")
        params.append(material_code)
    if warehouse:
        where.append("warehouse = ?")
        params.append(warehouse)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM stock WHERE {clause}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM stock WHERE {clause} ORDER BY {sort_column} {direction}, id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [stock_out(r) for r in rows], total


def list_ledger(conn, *, material_code=None, txn_type=None, work_order_no=None, offset=0, limit=20):
    where, params = ["1=1"], []
    if material_code:
        where.append("material_code = ?")
        params.append(material_code)
    if txn_type:
        where.append("txn_type = ?")
        params.append(txn_type)
    if work_order_no:
        where.append("work_order_no = ?")
        params.append(work_order_no)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM stock_ledger WHERE {clause}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM stock_ledger WHERE {clause} ORDER BY id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [ledger_out(r) for r in rows], total


# ==================== 物料主数据 ====================
def create_material(conn, payload, current_user):
    code = payload.material_code.strip()
    if conn.execute("SELECT 1 FROM materials WHERE material_code = ?", (code,)).fetchone():
        raise MaterialCodeDuplicate(f"物料编码已存在: {code}", material_code=code)
    price = parse_cent(payload.unit_price)
    safety = parse_qty_allow_zero(payload.safety_stock, "safety_stock")
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO materials (material_code, name, category, unit, unit_price_cent,"
        " safety_stock_milli, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code, payload.name, payload.category, payload.unit, price, safety, now, now),
    )
    row = conn.execute("SELECT * FROM materials WHERE id = ?", (cur.lastrowid,)).fetchone()
    return material_out(row)


def update_material(conn, material_code: str, payload, current_user):
    row = conn.execute(
        "SELECT * FROM materials WHERE material_code = ? AND deleted_at IS NULL",
        (material_code,),
    ).fetchone()
    if row is None:
        raise NotFound(f"物料不存在或已删除: {material_code}", material_code=material_code)
    _check_version("物料", row["version"], payload.version)

    updates, params = [], []
    if payload.name is not None:
        updates.append("name = ?")
        params.append(payload.name)
    if payload.category is not None:
        updates.append("category = ?")
        params.append(payload.category)
    if payload.unit_price is not None:
        updates.append("unit_price_cent = ?")
        params.append(parse_cent(payload.unit_price))
    if payload.safety_stock is not None:
        updates.append("safety_stock_milli = ?")
        params.append(parse_qty_allow_zero(payload.safety_stock, "safety_stock"))
    if payload.status is not None:
        if payload.status not in ("ACTIVE", "INACTIVE"):
            raise ValidationFailed(f"status 只能取 ACTIVE/INACTIVE，实际 {payload.status!r}")
        updates.append("status = ?")
        params.append(payload.status)
    if not updates:
        raise ValidationFailed("没有提供任何要更新的字段")

    updates.append("version = version + 1")
    updates.append("updated_at = ?")
    params.extend([now_iso(), material_code])
    conn.execute(f"UPDATE materials SET {', '.join(updates)} WHERE material_code = ?", params)
    return material_out(conn.execute("SELECT * FROM materials WHERE material_code = ?", (material_code,)).fetchone())


def delete_material(conn, material_code: str, current_user):
    """软删除。硬删除会打断历史单据的追溯链，真实系统一律软删。"""
    row = conn.execute(
        "SELECT * FROM materials WHERE material_code = ? AND deleted_at IS NULL",
        (material_code,),
    ).fetchone()
    if row is None:
        raise NotFound(f"物料不存在或已删除: {material_code}", material_code=material_code)

    referenced = conn.execute(
        "SELECT COUNT(*) AS c FROM stock_ledger WHERE material_code = ?",
        (material_code,),
    ).fetchone()["c"]
    if referenced > 0:
        raise MaterialInUse(
            f"物料 {material_code} 已有 {referenced} 条出入库流水，不允许删除",
            material_code=material_code,
            ledger_count=referenced,
        )

    now = now_iso()
    conn.execute(
        "UPDATE materials SET deleted_at = ?, updated_at = ?, version = version + 1 WHERE material_code = ?",
        (now, now, material_code),
    )
    return material_out(conn.execute("SELECT * FROM materials WHERE material_code = ?", (material_code,)).fetchone())


def list_materials(
    conn,
    *,
    keyword=None,
    category=None,
    status=None,
    include_deleted: bool = False,
    sort_column="id",
    direction="ASC",
    offset=0,
    limit=20,
):
    where, params = ["1=1"], []
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if category:
        where.append("category = ?")
        params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    if keyword:
        where.append("(material_code LIKE ? OR name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) AS c FROM materials WHERE {clause}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM materials WHERE {clause} ORDER BY {sort_column} {direction}, id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [material_out(r) for r in rows], total


def get_material(conn, material_code: str) -> dict:
    row = conn.execute(
        "SELECT * FROM materials WHERE material_code = ? AND deleted_at IS NULL",
        (material_code,),
    ).fetchone()
    if row is None:
        raise NotFound(f"物料不存在或已删除: {material_code}", material_code=material_code)
    return material_out(row)


def work_order_ledger(conn, order_no: str):
    """按工单聚合的出库流水，用于追溯"这批料到底用在了哪个工单上"。"""
    wo = get_work_order_row(conn, order_no)
    rows = conn.execute(
        "SELECT * FROM stock_ledger WHERE work_order_no = ? ORDER BY id ASC",
        (order_no,),
    ).fetchall()
    entries = rows_to_dicts(rows)
    total_amount_cent = 0
    for e in entries:
        mat = conn.execute(
            "SELECT unit_price_cent FROM materials WHERE material_code = ?",
            (e["material_code"],),
        ).fetchone()
        if mat:
            total_amount_cent += amount_cent_int(e["qty_milli"], mat["unit_price_cent"])
    return {
        "order_no": wo["order_no"],
        "status": wo["status"],
        "entries": [ledger_out(r) for r in rows],
        "total_amount": amount_cent_to_str(total_amount_cent),
    }


def amount_cent_int(qty_milli: int, unit_price_cent: int) -> int:
    return (qty_milli * unit_price_cent + 500) // 1000


def amount_cent_to_str(cent: int) -> str:
    return cent_to_str(cent)


def table_counts() -> dict[str, int]:
    """各表行数快照。用例做隔离性校验时用：一个用例跑完行数必须回到基线。"""
    from mock_server.db import table_counts as _counts

    return _counts()


def all_tables_snapshot() -> dict[str, int]:
    return table_counts()


__all__ = [
    "ALLOWED_TRANSITIONS",
    "TRANSITION_GUARDS",
    "WORK_ORDER_SORTABLE",
    "MATERIAL_SORTABLE",
    "PRIORITIES",
    "add_batch",
    "approve_work_order",
    "create_inspection",
    "create_material",
    "create_work_order",
    "delete_material",
    "get_material",
    "get_work_order_row",
    "issue_material",
    "list_ledger",
    "list_materials",
    "list_stock",
    "list_work_orders",
    "report_production",
    "reverse_material",
    "start_batch",
    "stock_in_finished_goods",
    "transition_work_order",
    "update_material",
    "work_order_ledger",
]

# 注意：`from mock_server.services import *` 只会导出上面 __all__ 里的名字。
# amount_str 在 mock_server/quantity.py、random_suffix 在 mock_server/util.py，
# 它们不属于本模块，曾一度被误列进 __all__，导致
# F822 Undefined name 报错，也让人以为 services 里有这两个函数。
