"""openapi.json 的导出/漂移门禁，以及「自动导出欠声明」这一缺陷的显式钉住。

这个文件同时做两件事，注意它们的性质完全不同：

1. **门禁**（`test_export_script_*`）：验证 `scripts/export_openapi.py --check` 真的能挡住漂移。
   门禁本身也要有测试，否则"CI 里有一步"只是一句话。
2. **缺陷钉住**（`@pytest.mark.xfail(strict=True)` 的那些）：断言"契约**应该**声明的东西"。
   它们现在必然失败（所以是 xfail），一旦有人把欠声明补上就会变成 XPASS，
   在 strict 模式下直接报红——逼着改的人回来删掉 marker 并更新文档。

被钉住的缺陷编号与说明见 `docs/wave1-contract-handoff.md` 的「真实缺陷清单」。
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import requests
from spec_contract import (
    LOGIN_SPEC_PATH,
    PRISTINE_SPEC_PATH,
    PUBLIC_OPERATIONS,
    declared_status_codes,
    iter_operations,
    operation_ids,
)

pytestmark = pytest.mark.contract

ROOT = PRISTINE_SPEC_PATH.parent.parent
EXPORT_SCRIPT = ROOT / "scripts" / "export_openapi.py"

# 实测出来的「服务端真的会返回、但纯净 spec 一条都没声明」的状态码。
# 每一项都是一个真实复现（见 test_live_error_status_codes_are_reachable），
# 不是从代码推出来的猜测。
UNDECLARED_BUT_REACHABLE: list[tuple[str, int]] = [
    ("GET /api/v1/materials", 401),  # deps.py:34 无 Bearer 令牌
    ("POST /api/v1/materials", 403),  # security.py PERMISSIONS：QA 无 material:create
    ("GET /api/v1/materials", 400),  # deps.py:114 page<1
    ("GET /api/v1/materials/{material_code}", 404),  # 物料不存在
    ("PUT /api/v1/materials/{material_code}", 409),  # 乐观锁 version 不匹配
    ("POST /api/v1/auth/login", 401),  # 口令错
]


def run_export(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


# ==================== 门禁本身 ====================
def test_export_script_check_reports_no_drift() -> None:
    """入库文件与代码当前一致（这是 CI 里那一步的本地版本）。"""
    result = run_export("--check")
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "[一致]" in result.stdout


def test_export_script_detects_tampered_file(tmp_path: Path) -> None:
    """把入库文件改一个字节，门禁必须非零退出并给出差异摘要。"""
    for source in (PRISTINE_SPEC_PATH, LOGIN_SPEC_PATH):
        shutil.copy(source, tmp_path / source.name)
    target = tmp_path / PRISTINE_SPEC_PATH.name
    tampered = json.loads(target.read_text(encoding="utf-8"))
    tampered["info"]["version"] = "9.9.9"
    target.write_text(json.dumps(tampered, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    result = run_export("--check", "--out-dir", str(tmp_path))
    assert result.returncode == 1
    assert "[漂移]" in result.stderr
    assert "9.9.9" in result.stderr or "1.0.0" in result.stderr


def test_export_script_detects_missing_file(tmp_path: Path) -> None:
    """入库文件不存在时也必须非零退出（新增应用忘了提交产物的场景）。"""
    result = run_export("--check", "--out-dir", str(tmp_path))
    assert result.returncode == 1
    assert "不存在" in result.stderr


# ==================== 纯净视图的形状（现状取证） ====================
def test_pristine_spec_shape(pristine_spec: dict[str, Any]) -> None:
    paths = pristine_spec["paths"]
    operations = iter_operations(pristine_spec)
    assert pristine_spec["openapi"] == "3.1.0"
    assert len(paths) == 23
    assert len(operations) == 29
    assert all(path.startswith("/api/v1") for path in paths)
    assert "servers" not in pristine_spec
    assert {"GET /api/v1/healthz", "GET /api/v1/readyz", "POST /api/v1/auth/login"} <= set(operation_ids(pristine_spec))


# ==================== DEF-02a：鉴权在 spec 里没有语义 ====================
@pytest.mark.xfail(
    strict=True,
    reason="DEF-02a：鉴权写成 authorization: str | None = Header(...)（mock_server/deps.py:34），"
    "FastAPI 只能导出成可选普通 header 参数，导不出 securitySchemes/security 语义。"
    "见 docs/wave1-contract-handoff.md「真实缺陷清单」DEF-02a。",
)
def test_spec_declares_bearer_auth_scheme(pristine_spec: dict[str, Any]) -> None:
    assert "securitySchemes" in pristine_spec.get("components", {})
    protected = [
        f"{method} {path}" for method, path, operation in iter_operations(pristine_spec) if operation.get("security")
    ]
    assert len(protected) == len(operation_ids(pristine_spec)) - len(PUBLIC_OPERATIONS)


# ==================== DEF-02b：可达的错误状态码一条都没声明 ====================
@pytest.mark.parametrize(("op_id", "status"), UNDECLARED_BUT_REACHABLE, ids=lambda v: str(v))
@pytest.mark.xfail(
    strict=True,
    reason="DEF-02b：400/401/403/404/409 由 mock_server/app.py 的全局 exception_handler 产出，"
    "FastAPI 生成器看不到，纯净 spec 只声明 200/201/422。见 handoff「真实缺陷清单」DEF-02b。",
)
def test_reachable_error_status_is_declared_in_spec(pristine_spec: dict[str, Any], op_id: str, status: int) -> None:
    method, path = op_id.split(" ", 1)
    operation = pristine_spec["paths"][path][method.lower()]
    assert str(status) in declared_status_codes(operation)


# ==================== DEF-02 的前提：这些码真的会返回 ====================
def test_live_error_status_codes_are_reachable(contract_server: Any, tokens: dict[str, str]) -> None:
    """逐条复现「未声明但真实可达」的状态码——这是 DEF-02 的证据，必须一直成立。"""
    base = contract_server.base_url
    admin = {"Authorization": f"Bearer {tokens['ADMIN']}"}
    qa = {"Authorization": f"Bearer {tokens['QA']}"}
    session = requests.Session()

    cases: list[tuple[str, int, requests.Response]] = [
        ("GET /api/v1/materials 无令牌", 401, session.get(f"{base}/materials", timeout=10)),
        (
            "POST /api/v1/materials 用 QA 令牌",
            403,
            session.post(
                f"{base}/materials",
                json={
                    "material_code": "MAT-CONTRACT-1",
                    "name": "n",
                    "category": "c",
                    "unit": "KG",
                    "unit_price": "1",
                },
                headers=qa,
                timeout=10,
            ),
        ),
        (
            "GET /api/v1/materials?page=0",
            400,
            session.get(f"{base}/materials", params={"page": 0}, headers=admin, timeout=10),
        ),
        ("GET /api/v1/materials/{不存在}", 404, session.get(f"{base}/materials/NOT-EXIST", headers=admin, timeout=10)),
        (
            "PUT /api/v1/materials/MAT-CU-001 version=999",
            409,
            session.put(f"{base}/materials/MAT-CU-001", json={"version": 999, "name": "x"}, headers=admin, timeout=10),
        ),
        (
            "POST /api/v1/auth/login 口令错",
            401,
            session.post(f"{base}/auth/login", json={"username": "admin", "password": "wrong"}, timeout=10),
        ),
    ]
    for label, expected, resp in cases:
        assert resp.status_code == expected, f"{label}: 期望 {expected}，实际 {resp.status_code} {resp.text[:200]}"
    # 400/409/403/404 的响应体都用统一错误信封，不是 FastAPI 默认的 {"detail": [...]}
    for _, _, resp in cases:
        body = resp.json()
        assert set(body) >= {"code", "message", "detail"}, f"{resp.request.url} 响应体不是统一错误信封：{body}"


# ==================== DEF-01：422 响应体违反 spec 自己声明的 schema ====================
def _declared_422_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """把 spec 为 422 声明的 HTTPValidationError 内联展开（它只引用 ValidationError）。"""
    schemas = spec["components"]["schemas"]
    declared = copy.deepcopy(schemas["HTTPValidationError"])
    declared["properties"]["detail"]["items"] = copy.deepcopy(schemas["ValidationError"])
    return declared


def test_live_422_body_is_stable_error_envelope(contract_server: Any, admin_token: str) -> None:
    """现状取证：服务端的 422 用的是自己的错误信封，detail 是对象或 null。"""
    resp = requests.post(
        f"{contract_server.base_url}/materials",
        json={"material_code": "MAT-CONTRACT-2"},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "REQUEST_VALIDATION_ERROR"
    assert isinstance(body["detail"], dict)
    assert "errors" in body["detail"]


@pytest.mark.xfail(
    strict=True,
    reason="DEF-01：FastAPI 给 422 自动生成的 HTTPValidationError 是 {detail: 数组}，"
    "而 mock_server/app.py 的 RequestValidationError 处理器返回 {code, message, detail: {errors: [...]}}，"
    "两者不兼容。见 handoff「真实缺陷清单」DEF-01。",
)
def test_live_422_body_conforms_to_declared_schema(
    contract_server: Any, admin_token: str, pristine_spec: dict[str, Any]
) -> None:
    resp = requests.post(
        f"{contract_server.base_url}/materials",
        json={"material_code": "MAT-CONTRACT-3"},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10,
    )
    jsonschema.validate(instance=resp.json(), schema=_declared_422_schema(pristine_spec))


# ==================== DEF-03：文档宣称支持的表单登录实际不可用 ====================
def test_login_description_claims_form_support(pristine_spec: dict[str, Any]) -> None:
    """先钉住"文档确实这么宣称"——否则下面的缺陷就无据可依。"""
    description = pristine_spec["paths"]["/api/v1/auth/login"]["post"]["description"]
    assert "application/x-www-form-urlencoded" in description


@pytest.mark.parametrize(
    ("label", "payload", "content_type"),
    [
        ("正确凭据", "username=admin&password=admin123", "application/x-www-form-urlencoded"),
        ("缺少 password", "username=admin", "application/x-www-form-urlencoded"),
    ],
    ids=lambda v: str(v)[:20],
)
def test_declared_form_login_is_actually_rejected(
    contract_server: Any, label: str, payload: str, content_type: str
) -> None:
    """DEF-03 现状取证：声明的 form 登录拿不到 200，而是 422。

    机制：`login()` 的签名是 `body: dict = Body(default_factory=dict)`
    （mock_server/routers.py:79），FastAPI 在**进入函数体之前**就按 JSON 解析请求体，
    表单体一律先炸成 422 `dict_type`，走不到 routers.py:82 的 form 分支。
    """
    resp = requests.post(
        f"{contract_server.base_url}/auth/login",
        data=payload,
        headers={"Content-Type": content_type},
        timeout=10,
    )
    assert resp.status_code == 422, f"{label}：期望 422（现状），实际 {resp.status_code} {resp.text[:200]}"
    assert resp.json()["code"] == "REQUEST_VALIDATION_ERROR"


def test_empty_form_login_returns_500(contract_server: Any) -> None:
    """DEF-03 的第二半：空表单连 422 都拿不到，直接 500 内部错误。

    空体不触发 FastAPI 的 JSON 解析，才得以进入函数体走到 `await request.form()`；
    而 `python-multipart` 未安装，starlette 抛 AssertionError，被 app.py 的兜底处理器变成 500。
    """
    resp = requests.post(
        f"{contract_server.base_url}/auth/login",
        data="",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    assert resp.status_code == 500, f"期望 500（现状），实际 {resp.status_code} {resp.text[:200]}"
    assert resp.json()["detail"]["exception"] == "AssertionError"


@pytest.mark.xfail(
    strict=True,
    reason="DEF-03：routers.py:71-75 的 description 宣称同时支持 application/json 与 "
    "application/x-www-form-urlencoded，但 login() 的 body 参数让 FastAPI 在函数体外就按 JSON 解析，"
    "表单请求拿不到 200；空表单还会因为没装 python-multipart 变成 500。见 handoff DEF-03。",
)
def test_declared_form_login_returns_token(contract_server: Any) -> None:
    """缺陷修好后期望：表单登录应当和 JSON 登录一样拿到 200 + access_token。"""
    resp = requests.post(
        f"{contract_server.base_url}/auth/login",
        data={"username": "admin", "password": "admin123"},
        timeout=10,
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


# ==================== DEF-02a 的旁证：受保护端点靠 401 而不是靠 spec ====================
def test_protected_operations_are_not_public_in_fact(pristine_spec: dict[str, Any]) -> None:
    """纯净 spec 里 26 个受保护端点全都"看起来"不需要令牌（authorization 是可选参数）。"""
    looks_public = [
        f"{method} {path}"
        for method, path, operation in iter_operations(pristine_spec)
        if not any(
            p.get("in") == "header" and p.get("name") == "authorization" for p in operation.get("parameters", [])
        )
    ]
    assert sorted(looks_public) == sorted(PUBLIC_OPERATIONS)
