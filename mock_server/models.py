"""行 -> API 响应字典的映射。

单独放一层的原因：数量的"整数存储、字符串传输"转换只应该在一个地方发生。
如果每个接口各写各的 f"{x/1000}"，迟早出现"列表接口 3 位小数、详情接口 2 位小数"
这种前后端对不上的缺陷——而且这种缺陷在只断言状态码的用例里根本抓不到。
"""

from __future__ import annotations

from mock_server.quantity import amount_str, cent_to_str, qty_to_str

# 对外暴露的状态与中文名（前端直接展示，也方便用例断言）
WORK_ORDER_STATUS_TEXT = {
    "PENDING": "待审核",
    "APPROVED": "已审核",
    "SCHEDULED": "已排产",
    "IN_PROGRESS": "生产中",
    "COMPLETED": "已完工",
    "SHIPPED": "已发货",
    "CANCELLED": "已取消",
}

MATERIAL_STATUS_TEXT = {"ACTIVE": "启用", "INACTIVE": "停用"}

BATCH_STATUS_TEXT = {"CREATED": "已创建", "IN_PROGRESS": "生产中", "CLOSED": "已关闭"}


def user_out(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "display_name": row["display_name"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


def material_out(row) -> dict:
    return {
        "id": row["id"],
        "material_code": row["material_code"],
        "name": row["name"],
        "category": row["category"],
        "unit": row["unit"],
        "unit_price": cent_to_str(row["unit_price_cent"]),
        "safety_stock": qty_to_str(row["safety_stock_milli"]),
        "status": row["status"],
        "status_text": MATERIAL_STATUS_TEXT.get(row["status"], row["status"]),
        "deleted": row["deleted_at"] is not None,
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def work_order_out(row) -> dict:
    planned = row["planned_qty_milli"]
    good = row["reported_good_milli"]
    scrap = row["reported_scrap_milli"]
    # 良率保留 2 位小数百分比。除零也要处理——新建工单还没报工。
    total_reported = good + scrap
    yield_rate = f"{(good * 10000 // total_reported) / 100:.2f}" if total_reported else "0.00"
    return {
        "id": row["id"],
        "order_no": row["order_no"],
        "product_code": row["product_code"],
        "product_name": row["product_name"],
        "planned_qty": qty_to_str(planned),
        "reported_good_qty": qty_to_str(good),
        "reported_scrap_qty": qty_to_str(scrap),
        "stocked_in_qty": qty_to_str(row["stocked_in_qty_milli"]),
        "remaining_qty": qty_to_str(max(planned - good, 0)),
        "yield_rate": yield_rate,
        "status": row["status"],
        "status_text": WORK_ORDER_STATUS_TEXT.get(row["status"], row["status"]),
        "priority": row["priority"],
        "owner": row["owner"],
        "reviewer": row["reviewer"],
        "qc_result": row["qc_result"],
        "qc_note": row["qc_note"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def batch_out(row) -> dict:
    return {
        "id": row["id"],
        "batch_no": row["batch_no"],
        "work_order_id": row["work_order_id"],
        "planned_qty": qty_to_str(row["planned_qty_milli"]),
        "good_qty": qty_to_str(row["good_qty_milli"]),
        "scrap_qty": qty_to_str(row["scrap_qty_milli"]),
        "status": row["status"],
        "status_text": BATCH_STATUS_TEXT.get(row["status"], row["status"]),
        "line": row["line"],
        "operator": row["operator"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def inspection_out(row) -> dict:
    return {
        "id": row["id"],
        "work_order_id": row["work_order_id"],
        "result": row["result"],
        "inspect_qty": qty_to_str(row["inspect_qty_milli"]),
        "defect_qty": qty_to_str(row["defect_qty_milli"]),
        "note": row["note"],
        "inspector": row["inspector"],
        "created_at": row["created_at"],
    }


def stock_out(row) -> dict:
    # 可用 = 账面 - 占用 - 已发（已发料还在车间，不算可用）
    on_hand = row["available_milli"] + row["reserved_milli"] + row["issued_milli"]
    return {
        "material_code": row["material_code"],
        "warehouse": row["warehouse"],
        "available_qty": qty_to_str(row["available_milli"]),
        "reserved_qty": qty_to_str(row["reserved_milli"]),
        "issued_qty": qty_to_str(row["issued_milli"]),
        "on_hand_qty": qty_to_str(on_hand),
        "updated_at": row["updated_at"],
    }


def ledger_out(row) -> dict:
    return {
        "id": row["id"],
        "material_code": row["material_code"],
        "warehouse": row["warehouse"],
        "txn_type": row["txn_type"],
        "qty": qty_to_str(row["qty_milli"]),
        "balance_qty": qty_to_str(row["balance_milli"]),
        "work_order_no": row["work_order_no"],
        "batch_no": row["batch_no"],
        "operator": row["operator"],
        "remark": row["remark"],
        "created_at": row["created_at"],
    }


def unit_amount(row, qty_milli: int) -> str:
    """按物料单价算金额（整数运算，见 quantity.amount_str）。"""
    return amount_str(qty_milli, row["unit_price_cent"])
