"""契约面事实表，以及「测试用补全视图」的构造逻辑。

两份 spec 的区别（这是本模块最重要的一个设计决定）：

- **入库的 `docs/openapi.json`（下称纯净视图）**：`app.openapi()` 的逐字节产物，只受
  `scripts/export_openapi.py` 改写。它是契约漂移门禁的基准，因此**任何测试都不得修改它**。
- **测试用补全视图（test view）**：在内存里给纯净视图补上 FastAPI 自动生成器**结构上无法表达**、
  但服务端**确实会返回**的东西，只用于 schemathesis 的属性校验。补什么、为什么补，见
  `build_test_view()` 的逐条注释。

为什么不能直接改 `docs/openapi.json` 去补：那样漂移门禁就只剩一句"文件和它自己一致"，
既挡不住实现漂移，也掩盖了自动导出欠声明这一事实。欠声明本身是被显式钉住的（见
`test_contract_export.py`）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
PRISTINE_SPEC_PATH = ROOT / "docs" / "openapi.json"
LOGIN_SPEC_PATH = ROOT / "docs" / "openapi-login_app.json"
HANDWRITTEN_CONTRACT_PATH = ROOT / "testcases" / "login" / "contract.py"

# ==================== 属性测试规模 ====================
# schemathesis 3.39 没有内建的 max-examples 环境变量（constants.py 里只有 SCHEMATHESIS_HOOKS /
# SCHEMATHESIS_API_NAME / SCHEMATHESIS_BASE_URL / SCHEMATHESIS_WAIT_FOR_SCHEMA），
# 所以规模控制由本模块自己做。默认值刻意取小：CI 里必须几十秒内跑完，
# 想深挖就把 CONTRACT_MAX_EXAMPLES 调大，或 CONTRACT_FULL=1 用放大量。
DEFAULT_MAX_EXAMPLES = 8
FULL_MAX_EXAMPLES = 50


def max_examples() -> int:
    """每个 operation 的 hypothesis 样本数上限。"""
    raw = os.getenv("CONTRACT_MAX_EXAMPLES")
    if raw:
        return int(raw)
    if os.getenv("CONTRACT_FULL", "").lower() in {"1", "true", "yes"}:
        return FULL_MAX_EXAMPLES
    return DEFAULT_MAX_EXAMPLES


def negative_max_examples() -> int:
    """负向样本数单独一份：负向用例触发写操作的概率更高，默认更少。"""
    raw = os.getenv("CONTRACT_NEGATIVE_MAX_EXAMPLES")
    if raw:
        return int(raw)
    return max(2, max_examples() // 2)


# ==================== 契约面事实表 ====================
# 事实来源：mock_server/routers.py 的 @router 装饰器与 Depends，逐条 grep 核对。
# 键是 "<METHOD> <path>"，与 spec 里的 path 一字不差（含 /api/v1 前缀）。

# 无鉴权端点：routers.py:55 healthz / routers.py:62 readyz / routers.py:70 auth/login
PUBLIC_OPERATIONS = frozenset(
    {
        "GET /api/v1/healthz",
        "GET /api/v1/readyz",
        "POST /api/v1/auth/login",
    }
)

# 需要的权限动作（mock_server/security.py 的 PERMISSIONS 键）。
# None = 只要求"已登录"，不要求具体动作（对应依赖是 get_current_user）。
REQUIRED_ACTIONS: dict[str, str | None] = {
    "GET /api/v1/auth/me": None,
    "POST /api/v1/materials": "material:create",
    "GET /api/v1/materials": None,
    "GET /api/v1/materials/{material_code}": None,
    "PUT /api/v1/materials/{material_code}": "material:update",
    "DELETE /api/v1/materials/{material_code}": "material:delete",
    "POST /api/v1/work-orders": "work_order:create",
    "GET /api/v1/work-orders": "work_order:read",
    "GET /api/v1/work-orders/{order_no}": "work_order:read",
    "POST /api/v1/work-orders/{order_no}/approve": "work_order:approve",
    "POST /api/v1/work-orders/{order_no}/batches": "batch:create",
    "GET /api/v1/work-orders/{order_no}/batches": "work_order:read",
    "GET /api/v1/work-orders/{order_no}/inspections": "work_order:read",
    "POST /api/v1/work-orders/{order_no}/batches/{batch_no}/start": "batch:start",
    "POST /api/v1/work-orders/{order_no}/report": "production:report",
    "POST /api/v1/work-orders/{order_no}/inspections": "inspection:create",
    "POST /api/v1/work-orders/{order_no}/stock-in": "production:stock_in",
    "POST /api/v1/work-orders/{order_no}/complete": "production:report",
    "POST /api/v1/work-orders/{order_no}/schedule": "work_order:schedule",
    "POST /api/v1/work-orders/{order_no}/ship": "work_order:ship",
    "POST /api/v1/work-orders/{order_no}/cancel": "work_order:cancel",
    "GET /api/v1/inventory/stock": None,
    "POST /api/v1/inventory/issue": "inventory:issue",
    "POST /api/v1/inventory/reverse": "inventory:reverse",
    "GET /api/v1/inventory/ledger": None,
    "GET /api/v1/work-orders/{order_no}/traceability": "work_order:read",
}

ROLES = ("ADMIN", "PLANNER", "OPERATOR", "QA")

# 种子账号，来源 mock_server/db.py:177-184 的 SEED_USERS（权威来源，不读 apis/）。
# 不依赖 apis/mes_api.py 是因为那边正在被改成从环境变量取口令。
SEED_CREDENTIALS: dict[str, tuple[str, str]] = {
    "ADMIN": ("admin", "admin123"),
    "PLANNER": ("planner01", "plan123"),
    "OPERATOR": ("operator01", "op123"),
    "QA": ("qa01", "qa123"),
}

# 权限矩阵：动作 -> 允许的角色。来源 mock_server/security.py 的 PERMISSIONS。
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "material:create": ("ADMIN", "PLANNER"),
    "material:update": ("ADMIN", "PLANNER"),
    "material:delete": ("ADMIN",),
    "work_order:create": ("ADMIN", "PLANNER"),
    "work_order:read": ("ADMIN", "PLANNER", "OPERATOR", "QA"),
    "work_order:approve": ("ADMIN", "PLANNER"),
    "work_order:schedule": ("ADMIN", "PLANNER"),
    "work_order:cancel": ("ADMIN", "PLANNER"),
    "work_order:ship": ("ADMIN",),
    "batch:create": ("ADMIN", "PLANNER"),
    "batch:start": ("ADMIN", "OPERATOR"),
    "production:report": ("ADMIN", "OPERATOR"),
    "production:stock_in": ("ADMIN", "OPERATOR"),
    "inventory:issue": ("ADMIN", "OPERATOR"),
    "inventory:reverse": ("ADMIN", "OPERATOR"),
    "inspection:create": ("ADMIN", "QA"),
}

# 服务端**稳定错误信封**：mock_server/app.py 的四个 exception_handler 统一产出
# {code, message, detail}（detail 为对象或 null）。这是补全视图里错误响应用的 schema。
ERROR_ENVELOPE: dict[str, Any] = {
    "type": "object",
    "required": ["code", "message"],
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "detail": {"type": ["object", "null"]},
    },
}


def load_spec(path: Path) -> dict[str, Any]:
    """读一份入库 spec。"""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def iter_operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """展开成 [(METHOD, path, operation), ...]，按 path/method 排序保证稳定。"""
    result: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method.upper() not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                continue
            result.append((method.upper(), path, operation))
    return sorted(result)


def operation_ids(spec: dict[str, Any]) -> list[str]:
    """["GET /api/v1/healthz", ...]，用作 parametrize 的 id 与事实表的键。"""
    return [f"{method} {path}" for method, path, _ in iter_operations(spec)]


def declared_status_codes(operation: dict[str, Any]) -> set[str]:
    return set(operation.get("responses", {}))


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return dict(node)


def request_body_properties(spec: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    """取请求体 schema 的属性名集合（用于判定是否需要 409 版本冲突）。"""
    body = operation.get("requestBody")
    if not body:
        return set()
    for media in body.get("content", {}).values():
        schema = media.get("schema", {})
        if "$ref" in schema:
            schema = _resolve_ref(spec, schema["$ref"])
        props = schema.get("properties")
        if isinstance(props, dict):
            return set(props)
    return set()


def build_test_view(spec: dict[str, Any]) -> dict[str, Any]:
    """构造只给 schemathesis 用的补全视图（不写盘）。逐条补什么、为什么：

    1. `components.securitySchemes.bearerAuth` + 每个受保护 operation 的
       `security: [{bearerAuth: []}]`。为什么：鉴权写成 `authorization: str | None =
       Header(default=None)`（mock_server/deps.py:34），FastAPI 只能把它导出成一个
       **可选普通 header 参数**，导不出"没有令牌就是 401"这层语义。
    2. 删掉那个 `authorization` header 参数。为什么：它被第 1 条取代后若还留着，
       hypothesis 会给它生成随机值并当成一个"正当参数"来覆盖真实令牌。
    3. 补上服务端全局异常处理器**确实会返回**的状态码：401（受保护端点）、403（需要动作的
       端点）、400（有请求体或有 query 参数的端点）、404（有 path 参数的端点）、409
       （请求体含 `version`，即乐观锁）。为什么：这些码由 app.py 的 BizError /
       StarletteHTTPException 处理器统一产出，FastAPI 生成器看不到，纯净 spec 里
       一律缺失——那正是被钉住的那个缺陷（见 test_contract_export.py）。
    4. 把自动生成的 422 schema（`HTTPValidationError` = `{detail: 数组}`）换成服务端
       **实际**产出的错误信封。为什么：app.py 的 RequestValidationError 处理器返回
       `{code, message, detail:{errors:[...]}}`，与 HTTPValidationError 不兼容——这是
       缺陷 DEF-01，同样被显式钉住。
    """
    view: dict[str, Any] = json.loads(json.dumps(spec))
    view.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }

    for method, path, operation in iter_operations(view):
        op_id = f"{method} {path}"
        public = op_id in PUBLIC_OPERATIONS
        action = REQUIRED_ACTIONS.get(op_id)
        responses = operation.setdefault("responses", {})

        # 第 4 条：422 换成真实信封
        for code in list(responses):
            if code.startswith("4") or code.startswith("5"):
                responses[code] = {
                    "description": "服务端统一错误信封",
                    "content": {"application/json": {"schema": ERROR_ENVELOPE}},
                }

        if not public:
            # 第 1、2 条
            operation["security"] = [{"bearerAuth": []}]
            params = operation.get("parameters")
            if isinstance(params, list):
                operation["parameters"] = [
                    p for p in params if not (p.get("in") == "header" and p.get("name") == "authorization")
                ]
            responses["401"] = {
                "description": "缺少/无效令牌",
                "content": {"application/json": {"schema": ERROR_ENVELOPE}},
            }
        if action is not None:
            responses["403"] = {
                "description": "角色无该动作权限",
                "content": {"application/json": {"schema": ERROR_ENVELOPE}},
            }

        # 第 3 条：400 / 404 / 409
        has_query = any(p.get("in") == "query" for p in operation.get("parameters", []))
        has_body = "requestBody" in operation
        has_path_param = any(p.get("in") == "path" for p in operation.get("parameters", []))
        if has_query or has_body:
            responses["400"] = {
                "description": "业务校验失败（分页/排序/状态机等）",
                "content": {"application/json": {"schema": ERROR_ENVELOPE}},
            }
        if has_path_param:
            responses["404"] = {
                "description": "对象不存在",
                "content": {"application/json": {"schema": ERROR_ENVELOPE}},
            }
        if "version" in request_body_properties(view, operation):
            responses["409"] = {
                "description": "版本冲突 / 幂等键冲突",
                "content": {"application/json": {"schema": ERROR_ENVELOPE}},
            }
    return view


def roles_allowed(action: str) -> tuple[str, ...]:
    return PERMISSIONS[action]


# ==================== DEF-05 的定向分类（不是"关校验"） ====================
# mock_server/quantity.py:49 用 Decimal(str(value)) 解析数量/金额，但 except 只接
# (InvalidOperation, ValueError)。Decimal("INFINITY") 在 CPython 里**能构造成功**，
# 于是 `dec.quantize(...)` 在第 53 行抛 InvalidOperation，逃出 except，被
# app.py 的兜底处理器变成 500 INTERNAL_ERROR。同样逃逸的还有 1e999999 这类巨指数。
#
# 后果：任何字符串形状的数量/金额字段（qty / unit_price / planned_qty / good_qty ...）
# 只要值是非有限十进制字面量，服务就回 500——这是**服务的缺陷**（DEF-05）。
# 它由两个用例盯着：
#   - test_contract_export.py::test_non_finite_literal_returns_500 —— 取证，必须一直 500；
#   - test_schemathesis_properties.py::test_non_finite_decimal_literal_is_pinned —— strict xfail，
#     一旦修好就 XPASS 报红，逼人来删这个豁免。
#
# 属性测试**照常发送**这些输入，只是把因此产生的失败归类为"已知缺陷"而不是新信号。
# 这与"过滤掉不生成"有本质区别：输入真的发出去了，响应真的被看了，
# 只是断言时区分「已知缺陷」和「新问题」——后者一旦出现仍然报红。
NON_FINITE_LITERAL = re.compile(
    r"""^[+-]?(
        inf(inity)?                       # INF / INFINITY / Infinity
      | nan                               # NaN / nan
      | \d+(\.\d+)?[eE][+-]?\d{4,}        # 1e999999 这类巨指数
    )$""",
    re.VERBOSE | re.IGNORECASE,
)


def contains_non_finite_literal(node: Any) -> bool:
    """请求载荷里有没有非有限十进制字面量（DEF-05 的触发器）。"""
    if isinstance(node, str):
        return bool(NON_FINITE_LITERAL.match(node.strip()))
    if isinstance(node, dict):
        return any(contains_non_finite_literal(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(contains_non_finite_literal(value) for value in node)
    return False


def case_contains_non_finite_literal(case: Any) -> bool:
    """判定一个 schemathesis Case 是否踩中 DEF-05（看 body + 全部请求参数）。"""
    return contains_non_finite_literal(
        {"body": getattr(case, "body", None), "query": dict(case.query or {}), "path": dict(case.path_parameters or {})}
    )
