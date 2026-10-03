"""请求 / 响应契约（Pydantic）。

设计决策：**金额与数量一律用 str 字段，不用 float。**
pydantic 会因为 `float` 注解把 `"62.50"` 强转成 `62.5`，JSON 里再变成 62.5，
一路都在 IEEE754 里漂。用 str + Decimal 校验，精度责任明确。

设计决策：请求体默认 `extra="forbid"`。
多传一个字段就报 422，而不是静默忽略。为什么值得这么严：
真实事故里"后端字母拼错、前端照样 200"是最难查的一类——比如把 `quantity`
写成 `quantitiy`，静默忽略后服务端按 0 处理，单据金额直接归零。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """请求体：禁止多余字段，任何未定义字段都会 422。"""

    model_config = ConfigDict(extra="forbid")


class LenientModel(BaseModel):
    """响应体：允许后续新增字段（契约测试只防"删字段/改类型"）。"""

    model_config = ConfigDict(extra="ignore")


# ================= 鉴权 =================
class LoginIn(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(LenientModel):
    id: int
    username: str
    role: str
    display_name: str
    active: bool
    created_at: str


class TokenOut(LenientModel):
    access_token: str
    token_type: str
    expires_in: int
    user: UserOut


class ErrorOut(LenientModel):
    code: str
    message: str
    detail: dict | None = None


# ================= 物料主数据 =================
class MaterialCreateIn(StrictModel):
    material_code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=32)
    unit: str = Field(min_length=1, max_length=16)
    unit_price: str
    safety_stock: str = "0.000"


class MaterialUpdateIn(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = None
    unit_price: str | None = None
    safety_stock: str | None = None
    status: str | None = None
    # 乐观锁：改之前必须回传你读到的版本号
    version: int


class MaterialOut(LenientModel):
    id: int
    material_code: str
    name: str
    category: str
    unit: str
    unit_price: str
    safety_stock: str
    status: str
    status_text: str
    deleted: bool
    version: int
    created_at: str
    updated_at: str


class MaterialPage(LenientModel):
    items: list[MaterialOut]
    total: int
    page: int
    page_size: int
    pages: int


# ================= 工单 =================
class WorkOrderCreateIn(StrictModel):
    product_code: str = Field(min_length=2, max_length=64)
    product_name: str = Field(min_length=1, max_length=128)
    planned_qty: str
    priority: str = "NORMAL"
    owner: str = Field(default="", max_length=32)
    # 允许客户端指定单号，用于演示"单号唯一"的真实约束；
    # 不传则服务端生成。生产系统通常两种都支持（离线排产的场景要指定）。
    order_no: str | None = Field(default=None, max_length=32)


class VersionIn(StrictModel):
    """纯状态变更请求：只需要乐观锁版本号。"""

    version: int


class BatchCreateIn(StrictModel):
    batch_no: str | None = Field(default=None, max_length=32)
    planned_qty: str
    line: str = Field(min_length=1, max_length=32)
    operator: str = Field(default="", max_length=32)
    version: int


class BatchStartIn(StrictModel):
    version: int


class ReportIn(StrictModel):
    batch_no: str = Field(min_length=1, max_length=32)
    good_qty: str
    scrap_qty: str = "0.000"
    remark: str | None = Field(default=None, max_length=256)
    version: int


class InspectionIn(StrictModel):
    result: str
    inspect_qty: str
    defect_qty: str = "0.000"
    note: str | None = Field(default=None, max_length=256)
    # 质检不动工单状态，但真实系统里它同样有并发问题（两个人同时判定），
    # 所以也要求带版本号。
    version: int


class StockInIn(StrictModel):
    warehouse: str = Field(default="WH-FG", min_length=1, max_length=32)
    remark: str | None = Field(default=None, max_length=256)
    version: int


class WorkOrderOut(LenientModel):
    id: int
    order_no: str
    product_code: str
    product_name: str
    planned_qty: str
    reported_good_qty: str
    reported_scrap_qty: str
    stocked_in_qty: str
    remaining_qty: str
    yield_rate: str
    status: str
    status_text: str
    priority: str
    owner: str
    reviewer: str | None = None
    qc_result: str
    qc_note: str | None = None
    version: int
    created_at: str
    updated_at: str


class WorkOrderPage(LenientModel):
    items: list[WorkOrderOut]
    total: int
    page: int
    page_size: int
    pages: int


class BatchOut(LenientModel):
    id: int
    batch_no: str
    work_order_id: int
    planned_qty: str
    good_qty: str
    scrap_qty: str
    status: str
    status_text: str
    line: str
    operator: str
    version: int
    created_at: str
    updated_at: str


class InspectionOut(LenientModel):
    id: int
    work_order_id: int
    result: str
    inspect_qty: str
    defect_qty: str
    note: str | None = None
    inspector: str
    created_at: str


# ================= 库存 =================
class IssueIn(StrictModel):
    material_code: str = Field(min_length=2, max_length=64)
    warehouse: str = Field(default="WH-01", min_length=1, max_length=32)
    qty: str
    batch_no: str | None = Field(default=None, max_length=32)
    remark: str | None = Field(default=None, max_length=256)


class ReverseIn(StrictModel):
    material_code: str = Field(min_length=2, max_length=64)
    warehouse: str = Field(default="WH-01", min_length=1, max_length=32)
    qty: str
    batch_no: str | None = Field(default=None, max_length=32)
    remark: str | None = Field(default=None, max_length=256)


class StockOut(LenientModel):
    material_code: str
    warehouse: str
    available_qty: str
    reserved_qty: str
    issued_qty: str
    on_hand_qty: str
    updated_at: str


class LedgerOut(LenientModel):
    id: int
    material_code: str
    warehouse: str
    txn_type: str
    qty: str
    balance_qty: str
    work_order_no: str | None = None
    batch_no: str | None = None
    operator: str
    remark: str | None = None
    created_at: str


class LedgerPage(LenientModel):
    items: list[LedgerOut]
    total: int
    page: int
    page_size: int
    pages: int


class OperationResult(LenientModel):
    """状态变更类接口的通用响应：把最新状态和版本号回给客户端。"""

    ok: bool = True
    message: str
    work_order: WorkOrderOut | None = None
    batch: BatchOut | None = None
    inspection: InspectionOut | None = None
