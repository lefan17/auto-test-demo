"""FastAPI 依赖：鉴权、分页、排序白名单。

分页和排序的校验放在这一层，好处是"所有列表接口自动获得同样的保护"。
真实事故：`ORDER BY {前端传的字段}` 拼 SQL，攻击者传 `id; DROP TABLE`；
或者传一个不存在的字段导致数据库报 500。白名单是唯一正确的做法。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query, status

from mock_server.db import read_tx
from mock_server.errors import PermissionDenied
from mock_server.models import user_out
from mock_server.security import decode_token, ensure_permission

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str
    display_name: str

    def require(self, action: str) -> None:
        ensure_permission(self.role, action)


def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """从 Authorization: Bearer <token> 解析当前用户。

    401 走 HTTPException（FastAPI 原生），业务类错误才走 BizError。
    这样"没登录"和"没权限"在测试里是两种完全不同的断言。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHENTICATED", "message": "缺少 Bearer 令牌"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_INVALID", "message": "令牌无效或已过期"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    with read_tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (payload["uid"],)).fetchone()
    if row is None or not row["active"]:
        # 令牌签发后被停用的账号：即使签名有效也不放行。
        # 这是权限体系里最容易被漏掉的一条——"离职员工的旧 token 还能用"。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "USER_DISABLED", "message": "账号不存在或已停用"},
        )
    user = user_out(row)
    return CurrentUser(
        id=user["id"],
        username=user["username"],
        role=user["role"],
        display_name=user["display_name"],
    )


def require_permission(action: str):
    """生成一个"先鉴权再解析请求体"的依赖。

    为什么要把权限做成依赖，而不是在函数体第一行调 `current.require(action)`：
    FastAPI 解析函数参数时**先做请求体校验、后执行函数体**。所以写在函数体里的
    权限检查永远排在 422 之后 —— 一个没有权限的角色只要少传一个字段，
    拿到的就是 422（参数缺失）而不是 403（没权限）。

    真实后果有两个：
    1. 越权者能通过错误信息的差异**探测接口的必填字段**，把接口结构一点点试出来；
    2. 客户端把 422 当成"我的请求写错了"，去改请求体而不是去申请权限，白费一轮。
    把鉴权放进 Depends 里，422/403 的先后顺序就固定成"没权限就是 403"。
    """

    def _check(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        current.require(action)
        return current

    return _check


@dataclass(frozen=True)
class Pagination:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def get_pagination(
    page: int = Query(default=1, description="页码，从 1 开始"),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, description=f"每页条数，最大 {MAX_PAGE_SIZE}"),
) -> Pagination:
    """分页参数校验。

    注意 page_size 超限这里返回 400（业务码 PAGE_SIZE_INVALID），
    不是 422。原因：422 是"参数类型都不对"，而这里类型是对的、
    只是业务不允许——两者让前端处理方式不同，混在一起会让前端没法区分。
    """
    from mock_server.errors import PageSizeInvalid, PaginationOutOfRange, ValidationFailed

    if page < 1:
        raise PaginationOutOfRange(f"page 必须 >= 1，实际 {page}")
    if page_size < 1:
        raise ValidationFailed(f"page_size 必须 >= 1，实际 {page_size}")
    if page_size > MAX_PAGE_SIZE:
        raise PageSizeInvalid(
            f"page_size 最大 {MAX_PAGE_SIZE}，实际 {page_size}；不限制会导致一次查询把整库拉出来",
            page_size=page_size,
            max_page_size=MAX_PAGE_SIZE,
        )
    return Pagination(page=page, page_size=page_size)


def resolve_sort(sort: str | None, allowed: dict[str, str], default: str) -> tuple[str, str]:
    """把排序参数解析成 (列名, ASC|DESC)。

    只允许出现在 `allowed` 白名单里的字段名；方向只允许 asc/desc。
    为什么必须白名单：这个值最终会被拼进 ORDER BY，是 SQL 注入的经典入口，
    而且"存在 SQL 注入"这件事任何扫描器都会报高危。
    """
    from mock_server.errors import SortFieldInvalid

    if not sort:
        column = allowed[default]
        return column, "ASC"

    field, _, direction = sort.partition(":")
    direction = (direction or "ASC").upper()
    if field not in allowed:
        raise SortFieldInvalid(
            f"排序字段 {field!r} 不在白名单内: {sorted(allowed)}",
            field=field,
            allowed=sorted(allowed),
        )
    if direction not in ("ASC", "DESC"):
        raise SortFieldInvalid(f"排序方向只支持 asc/desc，实际 {direction!r}")
    return allowed[field], direction


__all__ = [
    "CurrentUser",
    "Pagination",
    "PermissionDenied",
    "get_current_user",
    "get_pagination",
    "resolve_sort",
    "MAX_PAGE_SIZE",
]
