"""业务异常定义：每个错误都有稳定的错误码。

为什么要有错误码而不是只靠 HTTP 状态码：
同一个 400 可能对应十几种业务原因（库存不足 / 非法状态跃迁 / 质检未通过），
前端和自动化用例都靠 **code** 做精确断言。只断言状态码的用例，
在"错误原因被改错"时依然会绿——那是最容易漏掉的一类线上事故。
"""

from __future__ import annotations


class BizError(Exception):
    """业务异常基类。业务码写进响应体的 code 字段，供用例精确断言。"""

    code = "BIZ_ERROR"
    status = 400
    message = "业务处理失败"

    def __init__(self, message: str | None = None, **detail):
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)


class NotFound(BizError):
    code = "NOT_FOUND"
    status = 404
    message = "资源不存在"


class PermissionDenied(BizError):
    code = "PERMISSION_DENIED"
    status = 403
    message = "当前角色没有该操作权限"


# ---------- 400：请求格式合法，但不满足业务规则 ----------
class MaterialCodeDuplicate(BizError):
    code = "MATERIAL_CODE_DUPLICATE"
    status = 400
    message = "物料编码已存在"


class MaterialInUse(BizError):
    code = "MATERIAL_IN_USE"
    status = 400
    message = "物料已被工单引用，不允许删除"


class IllegalTransition(BizError):
    code = "ILLEGAL_STATUS_TRANSITION"
    status = 400
    message = "非法的状态跃迁"


class InsufficientStock(BizError):
    code = "INSUFFICIENT_STOCK"
    status = 400
    message = "可用库存不足"


class StockNotEnoughForReverse(BizError):
    code = "STOCK_NOT_ENOUGH_FOR_REVERSE"
    status = 400
    message = "退料数量超过已发料数量"


class BatchNotInProduction(BizError):
    code = "BATCH_NOT_IN_PRODUCTION"
    status = 400
    message = "批次未进入生产中状态，不允许报工"


class ReportExceedsPlan(BizError):
    code = "REPORT_EXCEEDS_PLAN"
    status = 400
    message = "报工数量超过计划数量"


class QualityGateFailed(BizError):
    code = "QUALITY_GATE_FAILED"
    status = 400
    message = "质检未通过，不允许入库"


class QuantityMismatch(BizError):
    code = "FINISHED_QTY_MISMATCH"
    status = 400
    message = "入库数量与计划数量不一致"


class BatchCodeInUse(BizError):
    code = "BATCH_CODE_IN_USE"
    status = 400
    message = "该批次号已存在于其它工单"


class PriorityInvalid(BizError):
    code = "PRIORITY_INVALID"
    status = 400
    message = "工单优先级取值非法"


class OrderNoDuplicate(BizError):
    code = "ORDER_NO_DUPLICATE"
    status = 400
    message = "工单号已存在"


class SortFieldInvalid(BizError):
    code = "SORT_FIELD_INVALID"
    status = 400
    message = "排序字段不在白名单内"


class PageSizeInvalid(BizError):
    code = "PAGE_SIZE_INVALID"
    status = 400
    message = "分页大小超出允许范围"


class PaginationOutOfRange(BizError):
    code = "PAGINATION_OUT_OF_RANGE"
    status = 400
    message = "分页参数超出允许范围"


# ---------- 409：并发 / 重复提交类冲突 ----------
class VersionConflict(BizError):
    code = "VERSION_CONFLICT"
    status = 409
    message = "数据已被他人修改，请刷新后重试"


class IdempotencyConflict(BizError):
    code = "IDEMPOTENCY_KEY_CONFLICT"
    status = 409
    message = "同一个幂等键被用于了不同的请求内容"


class IdempotencyInProgress(BizError):
    code = "IDEMPOTENCY_IN_PROGRESS"
    status = 409
    message = "完全相同的一次请求正在处理中"


class ConcurrentRequestRejected(BizError):
    code = "CONCURRENT_REQUEST_REJECTED"
    status = 409
    message = "并发请求被拒绝"


# ---------- 422：请求体本身不合法 ----------
class ValidationFailed(BizError):
    code = "VALIDATION_ERROR"
    status = 422
    message = "请求参数不合法"
