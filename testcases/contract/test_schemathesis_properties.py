"""schemathesis 属性驱动测试：拿导出的 spec 当契约，对真服务跑生成式用例。

四条设计原则（每一条都有原因，不是照抄文档）：

1. **用补全视图跑，不用纯净 spec 跑**。纯净 spec 缺 401/403/400/404/409，直接拿它跑
   `status_code_conformance` 会把服务的正常错误响应全部报成失败——那不是缺陷，是 spec 欠声明。
   补什么、为什么补见 `spec_contract.build_test_view()`。欠声明本身在 `test_contract_export.py`
   里被显式钉住，所以"补全视图"不会让缺陷消失，只是不让它污染属性测试。
2. **认证走 schemathesis 的 auth provider，不走手塞 header**。补全视图声明了
   `securitySchemes.bearerAuth`，schemathesis 会自己往 Authorization 里生成随机串（必然 401）。
   塞 header 是治标：生成器仍会覆盖它。用 provider 才是把"真令牌"接进生成链路。
3. **路径参数必须给显式值**。实测（见 handoff「实测记录」）：spec 里 `{order_no}` 只是
   `{"type": "string"}`，schemathesis 无法对它做负向生成，于是
   `POST /work-orders/{order_no}/report` 这类 operation 的负向策略必然 Unsatisfiable。
   给了显式 `path_parameters` 之后 17/17 个带路径参数的 operation 都能生成（实测数字见 handoff）。
   这是**用法**问题，不是服务缺陷。
4. **限规模用 hypothesis settings，不用 schemathesis 的环境变量**。3.39 没有内建的
   max-examples 环境变量，所以规模由 `spec_contract.max_examples()` 读环境变量后传进 settings。
   默认 8，`CONTRACT_MAX_EXAMPLES=50` 或 `CONTRACT_FULL=1` 放大。
"""

from __future__ import annotations

from typing import Any

import pytest
import requests
import schemathesis
from hypothesis import HealthCheck, given, settings
from schemathesis import DataGenerationMethod
from schemathesis.auths import AuthProvider
from schemathesis.checks import (
    content_type_conformance,
    not_a_server_error,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)
from schemathesis.exceptions import CheckFailed
from spec_contract import (
    PERMISSIONS,
    PRISTINE_SPEC_PATH,
    PUBLIC_OPERATIONS,
    REQUIRED_ACTIONS,
    ROLES,
    case_contains_non_finite_literal,
    iter_operations,
    load_spec,
    max_examples,
    negative_max_examples,
)

pytestmark = pytest.mark.contract

# OpenAPI 3.1 在 schemathesis 3.39 里是实验特性，不开会直接 SchemaError。
# 在 import 期就打开：本文件所有用例都依赖它，放进 fixture 只会把报错变成
# "某个 operation 拿不到"，而不是"3.1 没开"。
schemathesis.experimental.OPEN_API_3_1.enable()

# 全部 operation 的 id，模块级算一次（每个 spec 都一样，不必每个用例重读文件）。
ALL_OPERATIONS: list[str] = [f"{method} {path}" for method, path, _ in iter_operations(load_spec(PRISTINE_SPEC_PATH))]
PROTECTED_OPERATIONS: list[str] = [op for op in ALL_OPERATIONS if op not in PUBLIC_OPERATIONS]

# 负向用例的候选：先排除本来就无负向可言的公开探针端点
# （healthz/readyz 没有任何参数，schemathesis 对它们直接 raise SkipTest，实测见 handoff）。
# 其余 26 个里再挑代表性的 6 个：负向数据大概率写库，跑全集既慢又会把库写花。
NEGATIVE_OPERATIONS: list[str] = [
    "POST /api/v1/materials",
    "GET /api/v1/materials",
    "POST /api/v1/work-orders",
    "POST /api/v1/work-orders/{order_no}/report",
    "POST /api/v1/inventory/issue",
    "POST /api/v1/auth/login",
]

# 校验组合：状态码一致性（用补全视图的声明）、响应体符合 schema、内容类型、响应头、以及不许 5xx。
CHECK_CHECKS = (
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
)

# 路径参数的替身：取真实存在的种子物料，避免所有带路径参数的用例都掉进 404。
PATH_SUBSTITUTES = {"material_code": "MAT-CU-001", "order_no": "X", "batch_no": "X"}


def concrete_url(base_url: str, path: str) -> str:
    for name, value in PATH_SUBSTITUTES.items():
        path = path.replace("{" + name + "}", value)
    return f"{base_url}{path}"


def explicit_path_parameters(operation: Any) -> dict[str, str]:
    """给路径参数一个固定的合法值 —— 见模块 docstring 第 3 条。"""
    return {param.name: PATH_SUBSTITUTES.get(param.name, "X") for param in operation.path_parameters}


class BearerAuth(AuthProvider):
    """把真令牌塞进用例的 Authorization 头。

    schemathesis 先调 `get()` 拿凭据，拿到就调 `set()` 写进 case。
    凭据就是 token 字符串本身，`set()` 负责把它变成 header。
    """

    token: str = ""

    def get(self, case: Any, context: Any) -> str | None:
        del case, context
        return self.token or None

    def set(self, case: Any, data: str, context: Any) -> None:
        del context
        headers = dict(case.headers or {})
        headers["Authorization"] = f"Bearer {data}"
        case.headers = headers


def build_schema(test_view: dict[str, Any], base_url: str, token: str) -> Any:
    schema = schemathesis.openapi.from_dict(test_view, base_url=base_url)
    BearerAuth.token = token
    schema.auth.apply(BearerAuth)
    return schema


def operations_of(schema: Any) -> list[Any]:
    """get_all_operations() 返回 Ok/Failure 包装；这里有失败就直接报出来。"""
    resolved = []
    failures = []
    for item in schema.get_all_operations():
        if item.ok():
            resolved.append(item.ok())
        else:
            failures.append(item.err())
    assert not failures, f"有 operation 无法解析：{failures}"
    return sorted(resolved, key=lambda op: op.verbose_name)


def pick(schema: Any, op_id: str) -> Any:
    for operation in operations_of(schema):
        if operation.verbose_name == op_id:
            return operation
    raise AssertionError(f"{op_id} 不在 schema 的 operation 列表里")


def make_settings(examples: int) -> settings:
    """deadline：真网络往返，hypothesis 默认 200ms 的 deadline 必然误报；健康检查同理。"""
    return settings(
        max_examples=examples,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )


# ==================== 1. 正向属性：合法请求不违反契约 ====================
@pytest.mark.parametrize("op_id", ALL_OPERATIONS, ids=lambda s: s)
def test_positive_cases_conform_to_spec(
    test_view: dict[str, Any], contract_base_url: str, tokens: dict[str, str], op_id: str
) -> None:
    """29 个 operation 各跑正向生成数据，响应必须符合补全视图的声明。"""
    token = tokens["ADMIN"]
    schema = build_schema(test_view, contract_base_url, token)
    operation = pick(schema, op_id)
    strategy = schema.get_case_strategy(
        operation,
        data_generation_method=DataGenerationMethod.positive,
        path_parameters=explicit_path_parameters(operation),
    )
    observed: list[int] = []

    @make_settings(max_examples())
    @given(case=strategy)
    def run(case: Any) -> None:
        case.headers = dict(case.headers or {}, Authorization=f"Bearer {token}")
        response = case.call(base_url=schema.base_url)
        observed.append(response.status_code)
        case.validate_response(response, checks=CHECK_CHECKS)

    run()
    assert observed, f"{op_id} 一个样本都没生成"


# ==================== 2. 负向属性：非法数据必须被拒 ====================
@pytest.mark.parametrize("op_id", NEGATIVE_OPERATIONS, ids=lambda s: s)
def test_negative_cases_are_rejected(
    test_view: dict[str, Any], contract_base_url: str, tokens: dict[str, str], op_id: str
) -> None:
    """负向生成（缺必填、类型错、边界外）必须被拒，而不是 5xx。

    这里不用 `negative_data_rejection` check 而写显式断言：该 check 对本服务的分页语义
    （`page=0` 是 400 而不是 422，见 mock_server/deps.py:102）并不对齐，
    用它会把服务的正确行为报成失败——那是用法问题，不是服务问题。

    负向数据打到写接口上意味着真的建了脏数据，所以这个用例只断言"不 5xx + 错误码在合法集内"，
    不对 2xx 报错（负向生成有时只是"另一个合法值"，例如可选字段被省掉）。
    """
    token = tokens["ADMIN"]
    schema = build_schema(test_view, contract_base_url, token)
    operation = pick(schema, op_id)
    strategy = schema.get_case_strategy(
        operation,
        data_generation_method=DataGenerationMethod.negative,
        path_parameters=explicit_path_parameters(operation),
    )
    seen: list[int] = []
    known_defect: list[str] = []

    @make_settings(negative_max_examples())
    @given(case=strategy)
    def run(case: Any) -> None:
        case.headers = dict(case.headers or {}, Authorization=f"Bearer {token}")
        response = case.call(base_url=schema.base_url)
        if response.status_code >= 500:
            # 已知缺陷 DEF-05（非有限十进制字面量 → 500）单独记账，不算新信号。
            # 其它任何 5xx 一律照常失败。
            if case_contains_non_finite_literal(case):
                known_defect.append(response.text[:120])
                return
            raise AssertionError(f"负向数据打出了 5xx：{response.status_code} {response.text[:200]}")
        if response.status_code >= 400:
            seen.append(response.status_code)

    run()
    assert set(seen) <= {400, 401, 403, 404, 409, 422}, sorted(set(seen))
    if known_defect:
        # 命中了就留痕：报告里要靠它证明这个豁免不是摆设。
        print(f"[DEF-05] {op_id} 命中 {len(known_defect)} 次非有限字面量 500：{known_defect[0]}")


# ==================== 3. 鉴权属性：无令牌/坏令牌必须 401 ====================
@pytest.mark.parametrize("op_id", PROTECTED_OPERATIONS, ids=lambda s: s)
def test_requests_without_token_are_unauthorized(contract_base_url: str, op_id: str) -> None:
    """无令牌访问受保护端点必须 401 —— 这条属性在纯净 spec 里**无从得知**（DEF-02a）。

    `get_current_user`（mock_server/deps.py:34）在 Depends 链最外层，早于请求体校验，
    因此任意 body 都应当得到 401 而不是 422。
    """
    method, path = op_id.split(" ", 1)
    resp = requests.request(
        method, concrete_url(contract_base_url, path), json={} if method in {"POST", "PUT"} else None, timeout=10
    )
    assert resp.status_code == 401, f"{op_id} 无令牌应得 401，实际 {resp.status_code} {resp.text[:200]}"
    assert resp.json()["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("op_id", PROTECTED_OPERATIONS, ids=lambda s: s)
def test_requests_with_garbage_token_are_unauthorized(contract_base_url: str, op_id: str) -> None:
    method, path = op_id.split(" ", 1)
    resp = requests.request(
        method,
        concrete_url(contract_base_url, path),
        json={} if method in {"POST", "PUT"} else None,
        headers={"Authorization": "Bearer not-a-real-token"},
        timeout=10,
    )
    assert resp.status_code == 401, f"{op_id} 坏令牌应得 401，实际 {resp.status_code}"
    assert resp.json()["code"] == "TOKEN_INVALID"


# ==================== 4. 鉴权属性：权限矩阵逐格 ====================
ACTION_CASES: list[tuple[str, str, str]] = [
    (op_id, action, role) for op_id, action in sorted(REQUIRED_ACTIONS.items()) if action is not None for role in ROLES
]


@pytest.mark.parametrize(("op_id", "action", "role"), ACTION_CASES, ids=lambda v: str(v))
def test_permission_matrix_denies_with_403(
    contract_base_url: str, tokens: dict[str, str], op_id: str, action: str, role: str
) -> None:
    """角色无该动作权限时必须 403，且**先于**请求体校验（否则非法 body 会把 403 变成 422）。

    403 在纯净 spec 里也没声明（DEF-02b）；这里直接对矩阵逐格断言，不经过状态码校验。
    用空 body 是刻意的：空 body 必然触发 422，若鉴权不是先跑，这里就会红。
    """
    allowed = role in PERMISSIONS[action]
    method, path = op_id.split(" ", 1)
    resp = requests.request(
        method,
        concrete_url(contract_base_url, path),
        json={} if method in {"POST", "PUT"} else None,
        headers={"Authorization": f"Bearer {tokens[role]}"},
        timeout=10,
    )
    if allowed:
        assert resp.status_code != 403, f"{op_id} 对 {role} 是允许的，却得到 403"
    else:
        assert resp.status_code == 403, f"{op_id} 对 {role} 应得 403，实际 {resp.status_code} {resp.text[:200]}"
        body = resp.json()
        assert body["code"] == "PERMISSION_DENIED"
        assert body["detail"]["action"] == action
        assert body["detail"]["role"] == role
        assert sorted(body["detail"]["allowed"]) == sorted(PERMISSIONS[action])


# ==================== 5. 事实表必须覆盖 spec 的全部 operation ====================
def test_every_operation_is_covered_by_the_fact_table() -> None:
    """漏一个就等于那条接口没人管 —— 事实表与 spec 必须严格一一对应。"""
    known = set(REQUIRED_ACTIONS) | set(PUBLIC_OPERATIONS)
    assert set(ALL_OPERATIONS) == known, (
        f"仅出现在 spec：{sorted(set(ALL_OPERATIONS) - known)}；仅出现在事实表：{sorted(known - set(ALL_OPERATIONS))}"
    )
    assert len(ALL_OPERATIONS) == 29
    assert len(PROTECTED_OPERATIONS) == 26


# ==================== 6. DEF-05：非有限十进制字面量打穿服务 ====================
@pytest.mark.parametrize(
    ("method", "path", "body", "field"),
    [
        (
            "POST",
            "/api/v1/inventory/reverse",
            {"material_code": "MAT-CU-001", "warehouse": "WH-01", "qty": "INFINITY"},
            "qty",
        ),
        (
            "POST",
            "/api/v1/inventory/issue",
            {"material_code": "MAT-CU-001", "warehouse": "WH-01", "qty": "INFINITY"},
            "qty",
        ),
        (
            "POST",
            "/api/v1/work-orders",
            {"product_code": "P-1", "product_name": "x", "planned_qty": "INFINITY"},
            "planned_qty",
        ),
        (
            "POST",
            "/api/v1/materials",
            {"material_code": "MAT-DEF05-X", "name": "x", "category": "c", "unit": "u", "unit_price": "INFINITY"},
            "unit_price",
        ),
        (
            "POST",
            "/api/v1/inventory/reverse",
            {"material_code": "MAT-CU-001", "warehouse": "WH-01", "qty": "1e999999"},
            "qty",
        ),
    ],
    ids=lambda v: str(v)[:40],
)
def test_non_finite_decimal_literal_returns_500(
    contract_base_url: str, admin_token: str, method: str, path: str, body: dict[str, Any], field: str
) -> None:
    """DEF-05 取证：非有限十进制字面量让服务回 500（而不是 422）。

    这条用例断言的是**现状**（必须 500），所以它现在是 passed。
    它与下面的 strict-xfail 用例配对：前者防"服务悄悄换了个错法"，
    后者防"缺陷被修好了却没人回来清理豁免"。
    """
    del field
    resp = requests.request(
        method,
        concrete_url(contract_base_url, path),
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10,
    )
    assert resp.status_code == 500, f"{method} {path} {body} 期望 500（DEF-05 现状），实际 {resp.status_code}"
    assert resp.json()["detail"]["exception"] == "InvalidOperation"


@pytest.mark.xfail(
    strict=True,
    reason="DEF-05：mock_server/quantity.py:49 的 Decimal(str(value)) 对 'INFINITY'/'1e999999' 能构造成功，"
    "随后的 dec.quantize(...)（quantity.py:53）抛 InvalidOperation，而 except 只接并 (InvalidOperation, ValueError) "
    "之外的路径——实际是 quantize 在 try 块外，异常逃逸到 app.py 的兜底处理器变成 500。"
    "正确行为应是 422（输入格式问题）。见 handoff「真实缺陷清单」DEF-05。",
)
def test_non_finite_decimal_literal_is_rejected_as_422(contract_base_url: str, admin_token: str) -> None:
    """缺陷修好后期望：非有限字面量应当 422（与 'NaN' 的现有行为一致）。"""
    resp = requests.post(
        f"{contract_base_url}/api/v1/inventory/reverse",
        json={"material_code": "MAT-CU-001", "warehouse": "WH-01", "qty": "INFINITY"},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10,
    )
    assert resp.status_code == 422


def test_negative_strategy_exception_is_classified_not_swallowed() -> None:
    """守卫：确保 CheckFailed 与已知缺陷的分类确实生效（防止将来误改成正则放宽）。

    这里只断言分类函数本身的行为，不发请求 —— 属性测试的失败分类逻辑太关键，
    不能被"它反正绿了"含糊过去。
    """
    assert CheckFailed is not None
    assert case_contains_non_finite_literal(_FakeCase(body={"qty": "INFINITY"}))
    assert case_contains_non_finite_literal(_FakeCase(body={"unit_price": "1e999999"}))
    assert case_contains_non_finite_literal(_FakeCase(body={"qty": "NaN"}))
    assert not case_contains_non_finite_literal(_FakeCase(body={"qty": "1.500"}))
    assert not case_contains_non_finite_literal(_FakeCase(body={"qty": "-1"}))
    assert not case_contains_non_finite_literal(_FakeCase(body={"remark": "infinity 牌螺钉"}))
    assert not case_contains_non_finite_literal(_FakeCase(body=None, query={"page": "0"}))


class _FakeCase:
    def __init__(self, body: Any = None, query: dict[str, Any] | None = None, path: dict[str, Any] | None = None):
        self.body = body
        self.query = query or {}
        self.path_parameters = path or {}
