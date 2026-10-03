"""FastAPI 应用组装：路由挂载 + 统一的异常 -> 错误码映射。

为什么异常处理要单独一层、统一出口：
如果每个路由各自 try/except 拼错误响应，迟早出现"同一个业务错误在两个接口
里返回不同 code"。用例断言的是 code，一旦不一致，用例会红，但修的时候
要在几十个接口里找——统一出口把这件事收敛成一个地方。

统一响应约定（所有错误都长这样）：
    HTTP 4xx/5xx
    {"code": "INSUFFICIENT_STOCK", "message": "物料 ... 可用 120000，本次领用 200000",
     "detail": {"available": 120000, "requested": 200000}}
用例应该断言 `code`，因为 `message` 会随文案调整，`code` 才是契约。
"""

from __future__ import annotations

import os
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mock_server import db
from mock_server.errors import BizError
from mock_server.routers import router

API_PREFIX = os.getenv("MOCK_API_PREFIX", "/api/v1")


def create_app(db_path: str | None = None) -> FastAPI:
    if db_path:
        db.configure(db_path)
    db.ensure_ready()

    app = FastAPI(
        title="DSH MES Mock API",
        version="1.0.0",
        description=(
            "自动化测试用的 MES/WMS 业务后端（Mock）。\n\n"
            "刻意实现了真实系统才有的约束：唯一性、状态机、库存边界、"
            "乐观锁、幂等、角色权限、分页排序白名单、软删除。\n"
            "所有约束都在服务端真实生效并返回 4xx，不是只写在文档里。"
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.include_router(router, prefix=API_PREFIX)

    # ---------- 业务异常 ----------
    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError):
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail or None},
        )

    # ---------- 请求格式异常（pydantic 422）----------
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """把 pydantic 的错误压成稳定结构。

        注意把"多传了未定义字段"单独标出来（code=FIELD_NOT_ALLOWED）：
        这类问题在联调时最容易被当成"后端没生效"，实际上请求根本就没被受理。
        """
        errors = exc.errors()
        extra_fields = [e for e in errors if e.get("type") == "extra_forbidden"]
        code = "FIELD_NOT_ALLOWED" if extra_fields else "REQUEST_VALIDATION_ERROR"
        simplified = [
            {
                "field": ".".join(str(p) for p in e.get("loc", []) if p != "body"),
                "type": e.get("type"),
                "reason": e.get("msg"),
            }
            for e in errors
        ]
        return JSONResponse(
            status_code=422,
            content={"code": code, "message": "请求体校验失败", "detail": {"errors": simplified}},
        )

    # ---------- 401/403/404 等 HTTP 异常 ----------
    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            body = {
                "code": detail.get("code", "HTTP_ERROR"),
                "message": detail.get("message", ""),
                "detail": detail.get("detail"),
            }
        else:
            body = {"code": f"HTTP_{exc.status_code}", "message": str(detail), "detail": None}
        return JSONResponse(status_code=exc.status_code, content=body, headers=getattr(exc, "headers", None))

    # ---------- 未预期的 500 ----------
    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        """未捕获异常也要给出**机器可读**的响应。

        真实事故里"接口 500 但响应体是 HTML 错误页"会让前端 JSON 解析失败，
        报出与真实原因完全无关的错，排查时间全花在误导信息上。
        """
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "detail": {"exception": type(exc).__name__},
            },
        )

    return app


app = create_app()
