"""HTTP 路由。

这一层的职责很窄：解析请求 -> 开事务 -> 调服务层 -> 序列化响应。
业务规则一律不写在这里，更不写在路由的 if 里——否则"接口直接调服务层"的
用例和"通过 HTTP 调"的用例会走到两套逻辑，测试就失去意义了。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from mock_server import services
from mock_server.db import read_tx, write_tx
from mock_server.deps import (
    CurrentUser,
    get_current_user,
    get_pagination,
    require_permission,
    resolve_sort,
)
from mock_server.errors import ValidationFailed
from mock_server.idempotency import IdempotencyGuard, replay_or_none
from mock_server.models import user_out
from mock_server.schemas import (
    BatchCreateIn,
    BatchOut,
    BatchStartIn,
    InspectionIn,
    InspectionOut,
    IssueIn,
    LedgerPage,
    LoginIn,
    MaterialCreateIn,
    MaterialOut,
    MaterialPage,
    MaterialUpdateIn,
    OperationResult,
    ReportIn,
    ReverseIn,
    StockInIn,
    StockOut,
    TokenOut,
    VersionIn,
    WorkOrderCreateIn,
    WorkOrderOut,
    WorkOrderPage,
)
from mock_server.security import create_token, verify_password

router = APIRouter()


# ==================== 健康检查 ====================
@router.get("/healthz", tags=["system"], summary="存活探针")
def healthz():
    from mock_server.db import healthcheck

    return {"status": "UP", **healthcheck()}


@router.get("/readyz", tags=["system"], summary="就绪探针")
def readyz():
    with read_tx() as conn:
        conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return {"status": "READY"}


# ==================== 鉴权 ====================
@router.post(
    "/auth/login",
    response_model=TokenOut,
    tags=["auth"],
    summary="登录换取令牌",
    description=(
        "同时接受 `application/json` 和 `application/x-www-form-urlencoded`。"
        "真实系统的登录接口经常同时被前端(JSON)和运维脚本(表单)调用，"
        "只支持一种 Content-Type 会在联调时才发现问题。"
    ),
)
async def login(
    request: Request,
    body: dict = Body(default_factory=dict),
):
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload = {"username": form.get("username"), "password": form.get("password")}
    elif "application/json" in content_type:
        payload = body
    else:
        raise ValidationFailed(
            f"不支持的 Content-Type: {content_type!r}，只接受 application/json 或 application/x-www-form-urlencoded"
        )

    try:
        creds = LoginIn(**payload)
    except Exception as exc:  # pydantic ValidationError
        raise ValidationFailed(f"登录参数不合法: {exc}") from exc

    with read_tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (creds.username,)).fetchone()

    # 用户不存在和密码错误返回**同一个**错误码。
    # 分开提示等于给攻击者一个用户名枚举接口（"这个账号存在"本身就是情报）。
    if row is None or not verify_password(creds.password, row["password_hash"]):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误", "detail": None},
        )
    if not row["active"]:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"code": "USER_DISABLED", "message": "账号已停用", "detail": None},
        )

    token, ttl = create_token(row["id"], row["username"], row["role"])
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "user": user_out(row),
    }


@router.get("/auth/me", response_model=dict, tags=["auth"], summary="当前登录用户")
def me(current: CurrentUser = Depends(get_current_user)):
    return {
        "id": current.id,
        "username": current.username,
        "role": current.role,
        "display_name": current.display_name,
    }


# ==================== 物料主数据 ====================
@router.post(
    "/materials",
    response_model=MaterialOut,
    status_code=status.HTTP_201_CREATED,
    tags=["materials"],
    summary="新建物料",
)
def create_material(
    request: Request,
    payload: MaterialCreateIn,
    current: CurrentUser = Depends(require_permission("material:create")),
):
    guard = IdempotencyGuard(request, "material:create", payload.model_dump())
    with guard:
        replay = replay_or_none(guard)
        if replay is not None:
            return replay
        with write_tx() as conn:
            result = services.create_material(conn, payload, current)
        guard.store_response(201, result)
    return result


@router.get(
    "/materials",
    response_model=MaterialPage,
    tags=["materials"],
    summary="物料列表（分页/排序/过滤）",
)
def list_materials(
    keyword: str | None = Query(default=None, max_length=64, description="按编码或名称模糊搜索"),
    category: str | None = Query(default=None, max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    include_deleted: bool = Query(default=False, description="是否包含已软删除的物料"),
    sort: str | None = Query(default=None, description="排序，如 id:desc / unit_price:asc"),
    pagination=Depends(get_pagination),
    current: CurrentUser = Depends(get_current_user),
):
    column, direction = resolve_sort(sort, services.MATERIAL_SORTABLE, "id")
    with read_tx() as conn:
        items, total = services.list_materials(
            conn,
            keyword=keyword,
            category=category,
            status=status_filter,
            include_deleted=include_deleted,
            sort_column=column,
            direction=direction,
            offset=pagination.offset,
            limit=pagination.page_size,
        )
    pages = (total + pagination.page_size - 1) // pagination.page_size
    return {
        "items": items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": pages,
    }


@router.get(
    "/materials/{material_code}",
    response_model=MaterialOut,
    tags=["materials"],
    summary="物料详情",
)
def get_material(material_code: str, current: CurrentUser = Depends(get_current_user)):
    with read_tx() as conn:
        return services.get_material(conn, material_code)


@router.put(
    "/materials/{material_code}",
    response_model=MaterialOut,
    tags=["materials"],
    summary="修改物料（乐观锁）",
)
def update_material(
    material_code: str,
    payload: MaterialUpdateIn,
    current: CurrentUser = Depends(require_permission("material:update")),
):
    with write_tx() as conn:
        return services.update_material(conn, material_code, payload, current)


@router.delete(
    "/materials/{material_code}",
    response_model=MaterialOut,
    tags=["materials"],
    summary="删除物料（软删除）",
)
def delete_material(material_code: str, current: CurrentUser = Depends(require_permission("material:delete"))):
    with write_tx() as conn:
        return services.delete_material(conn, material_code, current)


# ==================== 工单 ====================
@router.post(
    "/work-orders",
    response_model=WorkOrderOut,
    status_code=status.HTTP_201_CREATED,
    tags=["work-orders"],
    summary="创建工单",
)
def create_work_order(
    request: Request,
    payload: WorkOrderCreateIn,
    current: CurrentUser = Depends(require_permission("work_order:create")),
):
    key = request.headers.get("Idempotency-Key")
    guard = IdempotencyGuard(request, "work_order:create", payload.model_dump())
    with guard:
        replay = replay_or_none(guard)
        if replay is not None:
            return replay
        with write_tx() as conn:
            result, _ = services.create_work_order(conn, current_user=current, payload=payload, idem_key=key)
        guard.store_response(201, result)
    return result


@router.get(
    "/work-orders",
    response_model=WorkOrderPage,
    tags=["work-orders"],
    summary="工单列表（分页/排序/过滤）",
)
def list_work_orders(
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    priority: str | None = Query(default=None, max_length=16),
    product_code: str | None = Query(default=None, max_length=64),
    owner: str | None = Query(default=None, max_length=32),
    include_cancelled: bool = Query(default=True),
    sort: str | None = Query(default=None, description="如 created_at:desc"),
    pagination=Depends(get_pagination),
    current: CurrentUser = Depends(get_current_user),
):
    current.require("work_order:read")
    column, direction = resolve_sort(sort, services.WORK_ORDER_SORTABLE, "id")
    with read_tx() as conn:
        items, total = services.list_work_orders(
            conn,
            status=status_filter,
            priority=priority,
            product_code=product_code,
            owner=owner,
            include_cancelled=include_cancelled,
            sort_column=column,
            direction=direction,
            offset=pagination.offset,
            limit=pagination.page_size,
        )
    pages = (total + pagination.page_size - 1) // pagination.page_size
    return {
        "items": items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": pages,
    }


@router.get(
    "/work-orders/{order_no}",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="工单详情",
)
def get_work_order(order_no: str, current: CurrentUser = Depends(require_permission("work_order:read"))):
    with read_tx() as conn:
        return services.work_order_row_out(conn, order_no)


@router.post(
    "/work-orders/{order_no}/approve",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="审核工单（PENDING -> APPROVED）",
)
def approve_work_order(
    order_no: str,
    payload: VersionIn,
    current: CurrentUser = Depends(require_permission("work_order:approve")),
):
    with write_tx() as conn:
        return services.approve_work_order(conn, order_no, payload.version, current)


@router.post(
    "/work-orders/{order_no}/batches",
    response_model=OperationResult,
    status_code=status.HTTP_201_CREATED,
    tags=["work-orders"],
    summary="为工单排产（创建生产批次）",
)
def add_batch(
    request: Request,
    order_no: str,
    payload: BatchCreateIn,
    current: CurrentUser = Depends(require_permission("batch:create")),
):
    guard = IdempotencyGuard(request, f"work_order:add_batch:{order_no}", payload.model_dump())
    with guard:
        replay = replay_or_none(guard)
        if replay is not None:
            return replay
        with write_tx() as conn:
            batch, _ = services.add_batch(conn, order_no, payload, current)
        body = {"ok": True, "message": f"批次 {batch['batch_no']} 已创建", "batch": batch}
        guard.store_response(201, body)
    return body


@router.get(
    "/work-orders/{order_no}/batches",
    response_model=list[BatchOut],
    tags=["work-orders"],
    summary="工单下的批次列表",
)
def list_batches(order_no: str, current: CurrentUser = Depends(require_permission("work_order:read"))):
    with read_tx() as conn:
        return services.list_batches(conn, order_no)


@router.get(
    "/work-orders/{order_no}/inspections",
    response_model=list[InspectionOut],
    tags=["work-orders"],
    summary="工单下的质检记录",
)
def list_inspections(order_no: str, current: CurrentUser = Depends(require_permission("work_order:read"))):
    with read_tx() as conn:
        return services.list_inspections(conn, order_no)


@router.post(
    "/work-orders/{order_no}/batches/{batch_no}/start",
    response_model=OperationResult,
    tags=["work-orders"],
    summary="批次开工（工单 -> IN_PROGRESS）",
)
def start_batch(
    order_no: str,
    batch_no: str,
    payload: BatchStartIn,
    current: CurrentUser = Depends(require_permission("batch:start")),
):
    with write_tx() as conn:
        wo, batch = services.start_batch(conn, order_no, batch_no, payload.version)
    return {"ok": True, "message": f"批次 {batch_no} 已开工", "work_order": wo, "batch": batch}


@router.post(
    "/work-orders/{order_no}/report",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="生产报工",
)
def report_production(
    order_no: str,
    payload: ReportIn,
    current: CurrentUser = Depends(require_permission("production:report")),
):
    with write_tx() as conn:
        return services.report_production(conn, order_no, payload, current)


@router.post(
    "/work-orders/{order_no}/inspections",
    response_model=OperationResult,
    status_code=status.HTTP_201_CREATED,
    tags=["work-orders"],
    summary="登记质检结果",
)
def create_inspection(
    request: Request,
    order_no: str,
    payload: InspectionIn,
    current: CurrentUser = Depends(require_permission("inspection:create")),
):
    guard = IdempotencyGuard(request, f"work_order:inspection:{order_no}", payload.model_dump())
    with guard:
        replay = replay_or_none(guard)
        if replay is not None:
            return replay
        with write_tx() as conn:
            wo, inspection = services.create_inspection(conn, order_no, payload, current)
        body = {
            "ok": True,
            "message": f"质检结果 {inspection['result']} 已登记",
            "work_order": wo,
            "inspection": inspection,
        }
        guard.store_response(201, body)
    return body


@router.post(
    "/work-orders/{order_no}/stock-in",
    response_model=OperationResult,
    tags=["work-orders"],
    summary="成品入库（IN_PROGRESS -> COMPLETED）",
)
def stock_in(
    order_no: str,
    payload: StockInIn,
    current: CurrentUser = Depends(require_permission("production:stock_in")),
):
    with write_tx() as conn:
        wo, stock = services.stock_in_finished_goods(conn, order_no, payload, current)
    return {"ok": True, "message": "成品已入库", "work_order": wo}


@router.post(
    "/work-orders/{order_no}/complete",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="工单完工（IN_PROGRESS -> COMPLETED）",
)
def complete_work_order(
    order_no: str,
    payload: VersionIn,
    current: CurrentUser = Depends(require_permission("production:report")),
):
    with write_tx() as conn:
        return services.transition_work_order(conn, order_no, "COMPLETED", payload.version, current)


@router.post(
    "/work-orders/{order_no}/schedule",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="工单排产（APPROVED -> SCHEDULED）",
)
def schedule_work_order(
    order_no: str,
    payload: VersionIn,
    current: CurrentUser = Depends(require_permission("work_order:schedule")),
):
    with write_tx() as conn:
        return services.transition_work_order(conn, order_no, "SCHEDULED", payload.version, current)


@router.post(
    "/work-orders/{order_no}/ship",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="工单发货（COMPLETED -> SHIPPED）",
)
def ship_work_order(
    order_no: str,
    payload: VersionIn,
    current: CurrentUser = Depends(require_permission("work_order:ship")),
):
    with write_tx() as conn:
        return services.transition_work_order(conn, order_no, "SHIPPED", payload.version, current)


@router.post(
    "/work-orders/{order_no}/cancel",
    response_model=WorkOrderOut,
    tags=["work-orders"],
    summary="取消工单",
)
def cancel_work_order(
    order_no: str,
    payload: VersionIn,
    current: CurrentUser = Depends(require_permission("work_order:cancel")),
):
    with write_tx() as conn:
        return services.transition_work_order(conn, order_no, "CANCELLED", payload.version, current)


# ==================== 库存 ====================
@router.get(
    "/inventory/stock",
    response_model=dict,
    tags=["inventory"],
    summary="库存余额列表",
)
def list_stock(
    material_code: str | None = Query(default=None, max_length=64),
    warehouse: str | None = Query(default=None, max_length=32),
    sort: str | None = Query(default=None),
    pagination=Depends(get_pagination),
    current: CurrentUser = Depends(get_current_user),
):
    allowed = {"material_code": "material_code", "warehouse": "warehouse", "available_qty": "available_milli"}
    column, direction = resolve_sort(sort, allowed, "material_code")
    with read_tx() as conn:
        items, total = services.list_stock(
            conn,
            material_code=material_code,
            warehouse=warehouse,
            sort_column=column,
            direction=direction,
            offset=pagination.offset,
            limit=pagination.page_size,
        )
    return {
        "items": items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": (total + pagination.page_size - 1) // pagination.page_size,
    }


@router.post(
    "/inventory/issue",
    response_model=StockOut,
    tags=["inventory"],
    summary="生产领料出库",
)
def issue_material(
    request: Request,
    payload: IssueIn,
    current: CurrentUser = Depends(require_permission("inventory:issue")),
):
    order_no = request.headers.get("X-Work-Order-No")
    guard = IdempotencyGuard(request, "inventory:issue", payload.model_dump())
    with guard:
        replay = replay_or_none(guard)
        if replay is not None:
            return replay
        with write_tx() as conn:
            result = services.issue_material(conn, payload, current, order_no=order_no)
        guard.store_response(200, result)
    return result


@router.post(
    "/inventory/reverse",
    response_model=StockOut,
    tags=["inventory"],
    summary="生产退料入库",
)
def reverse_material(
    request: Request,
    payload: ReverseIn,
    current: CurrentUser = Depends(require_permission("inventory:reverse")),
):
    order_no = request.headers.get("X-Work-Order-No")
    with write_tx() as conn:
        return services.reverse_material(conn, payload, current, order_no=order_no)


@router.get(
    "/inventory/ledger",
    response_model=LedgerPage,
    tags=["inventory"],
    summary="出入库流水（审计追溯）",
)
def list_ledger(
    material_code: str | None = Query(default=None, max_length=64),
    txn_type: str | None = Query(default=None, max_length=16),
    work_order_no: str | None = Query(default=None, max_length=32),
    pagination=Depends(get_pagination),
    current: CurrentUser = Depends(get_current_user),
):
    with read_tx() as conn:
        items, total = services.list_ledger(
            conn,
            material_code=material_code,
            txn_type=txn_type,
            work_order_no=work_order_no,
            offset=pagination.offset,
            limit=pagination.page_size,
        )
    return {
        "items": items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "pages": (total + pagination.page_size - 1) // pagination.page_size,
    }


@router.get(
    "/work-orders/{order_no}/traceability",
    response_model=dict,
    tags=["inventory"],
    summary="按工单追溯用料与金额",
)
def traceability(order_no: str, current: CurrentUser = Depends(require_permission("work_order:read"))):
    with read_tx() as conn:
        return services.work_order_ledger(conn, order_no)
