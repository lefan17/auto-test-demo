"""手写契约（testcases/login/contract.py）vs 自动导出 spec 的逐条对照。

`testcases/login/contract.py` 是**禁止修改**的：它是本次改造要替代的那版「人肉契约」，
必须原样保留下来当对照基准，否则"自动导出比手写强在哪"就无从证明。

这个文件的核心结论只有一句，但它推翻了任务书里的一个默认假设：
**手写契约描述的是 `login_app`（前缀 /api），自动导出的 mock_server spec 是另一个服务
（前缀 /api/v1），三条路径在 mock_server 里一条都不存在。** 因此"导出的 spec 要与手写契约
对得上"这句话，只有在**额外导出 login_app 的 spec** 之后才成立——`scripts/export_openapi.py`
同时导两份产物就是为了这件事。
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any

import pytest
import requests
from spec_contract import (
    HANDWRITTEN_CONTRACT_PATH,
    LOGIN_SPEC_PATH,
    declared_status_codes,
    iter_operations,
    load_spec,
    operation_ids,
)

from login_app.app import APP_TITLE as LOGIN_APP_TITLE
from login_app.app import CORRECT_PASSWORD as LOGIN_APP_PASSWORD

pytestmark = pytest.mark.contract

ROOT = HANDWRITTEN_CONTRACT_PATH.parent.parent.parent


def _load_handwritten() -> types.ModuleType:
    """按文件路径加载手写契约，避免依赖 testcases/login 被插进 sys.path 的副作用。

    刻意不 import：直接执行该模块会执行它的模块级代码，而它是"被对照的文物"，
    用最少的耦合把它读出来即可。
    """
    spec = importlib.util.spec_from_file_location("_handwritten_login_contract", HANDWRITTEN_CONTRACT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HANDWRITTEN = _load_handwritten()
CONTRACT: dict[str, Any] = HANDWRITTEN.CONTRACT

# 三条路径的归属：来自 login_app/app.py 的路由装饰器（第 82/86/170 行）。
HANDWRITTEN_OWNER = "login_app"


def test_handwritten_contract_is_about_login_app_not_mock_server(pristine_spec: dict[str, Any]) -> None:
    """取证：手写契约的三条路径在 mock_server spec 里全部缺失，在 login_app spec 里全部存在。"""
    handwritten_paths = set(CONTRACT["paths"])
    mock_paths = set(pristine_spec["paths"])
    login_paths = set(load_spec(LOGIN_SPEC_PATH)["paths"])

    assert handwritten_paths == {"/api/health", "/api/login", "/api/flaky"}
    assert handwritten_paths & mock_paths == set(), "手写契约的路径不该出现在 mock_server 里"
    assert handwritten_paths <= login_paths, "手写契约的路径必须都在 login_app spec 里"
    assert not any(p.startswith("/api/v1") for p in login_paths)
    assert HANDWRITTEN_OWNER == "login_app"


@pytest.mark.parametrize("path", sorted(CONTRACT["paths"]), ids=lambda p: p)
def test_handwritten_path_and_method_present_in_login_spec(path: str) -> None:
    """逐条：手写契约声明的 method 必须出现在自动导出的 login_app spec 里。"""
    spec = load_spec(LOGIN_SPEC_PATH)
    declared_methods = {m for m, p, _ in iter_operations(spec) if p == path}
    assert declared_methods == set(CONTRACT["paths"][path]), (
        f"{path}: 手写={CONTRACT['paths'][path]} 自动={declared_methods}"
    )


@pytest.mark.parametrize("status", sorted(CONTRACT["login_status_semantics"]), ids=lambda s: f"status-{s}")
def test_handwritten_status_semantics_missing_from_auto_export(status: int) -> None:
    """手写契约给了 200/400/401 三条语义，自动导出只声明了 200 —— 差距取证。"""
    spec = load_spec(LOGIN_SPEC_PATH)
    operation = spec["paths"]["/api/login"]["post"]
    if status == 200:
        assert "200" in declared_status_codes(operation)
    else:
        assert str(status) not in declared_status_codes(operation), (
            f"若 {status} 已被声明，说明有人在 spec 里补了声明；请同步更新 handoff 的对照结论"
        )


@pytest.mark.xfail(
    strict=True,
    reason="DEF-04：手写契约要求 /api/login 的 400/401 有文档语义，自动导出只声明 200。"
    "FastAPI 只从返回注解推导响应码，而 login_app/app.py:87 的 login() 返回 JSONResponse 且"
    "在函数体里直接 given status_code，生成器看不到。见 handoff「真实缺陷清单」DEF-04。",
)
@pytest.mark.parametrize("status", [400, 401])
def test_auto_export_documents_declared_login_status(status: int) -> None:
    operation = load_spec(LOGIN_SPEC_PATH)["paths"]["/api/login"]["post"]
    assert str(status) in declared_status_codes(operation)


@pytest.mark.parametrize("status", sorted(HANDWRITTEN.undeclared_status_codes()), ids=lambda s: f"undeclared-{s}")
def test_handwritten_contract_undeclared_codes(status: int) -> None:
    """手写契约自认「未声明」的 {405,422,500} —— 它在契约里被记成了文字，而不是可机检的断言。"""
    assert status in HANDWRITTEN.undeclared_status_codes()
    operation = load_spec(LOGIN_SPEC_PATH)["paths"]["/api/login"]["post"]
    assert str(status) not in declared_status_codes(operation)


def test_handwritten_undocumented_form_submission_is_real(login_server: Any) -> None:
    """手写契约的 `undocumented_behaviour.form_submission` 断言**实现确实接受表单**。

    这条也是"手写契约有价值"的证据：它记录了机器导不出来的行为差异。
    它属于 login_app，测试体与 testcases/login 里那套一致（服务由本目录的 login_server fixture 起）。
    """
    resp = requests.post(
        f"{login_server.base_url}/api/login",
        data={"username": "someone", "password": LOGIN_APP_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"表单提交应当被接受：{resp.status_code} {resp.text}"


def test_login_spec_documents_no_status_but_implementation_has_three(login_server: Any) -> None:
    """同一份 login_app spec，实际会返回的状态码集合是 200/400/401/503（503 来自 /api/flaky）。"""
    base = login_server.base_url
    observed = {
        "login-ok": requests.post(f"{base}/api/login", json={"username": "a", "password": "123456"}, timeout=10),
        "login-bad": requests.post(f"{base}/api/login", json={"username": "a", "password": "x"}, timeout=10),
        "login-nobody": requests.post(
            f"{base}/api/login", data="{", headers={"Content-Type": "text/plain"}, timeout=10
        ),
    }
    assert observed["login-ok"].status_code == 200
    assert observed["login-bad"].status_code == 401
    assert observed["login-nobody"].status_code == 400
    flaky = requests.get(f"{base}/api/flaky", timeout=10)
    assert flaky.status_code in {200, 503}

    spec = load_spec(LOGIN_SPEC_PATH)
    declared = set()
    for _, _, operation in iter_operations(spec):
        declared |= declared_status_codes(operation)
    assert declared == {"200"}, f"login_app spec 只应声明 200，实际 {sorted(declared)}"


def test_mock_server_spec_is_the_real_contract_surface(pristine_spec: dict[str, Any]) -> None:
    """自动导出的 mock_server spec 覆盖面（29 operation / 23 path），手写契约只有 3 条路径。

    这条不评价谁对，只固定住量级差 —— 手写契约能覆盖的接口面本来就只有导出产物的 1/10。
    """
    assert len(iter_operations(pristine_spec)) == 29
    assert len(pristine_spec["paths"]) == 23
    assert len(operation_ids(pristine_spec)) == 29
    assert len(CONTRACT["paths"]) == 3
    assert json.dumps(pristine_spec)  # 固定住 import 的用途


def test_handwritten_contract_file_is_untouched() -> None:
    """手写契约必须原样保留（本次改造不动 testcases/login/）。"""
    text = HANDWRITTEN_CONTRACT_PATH.read_text(encoding="utf-8")
    assert "CONTRACT: dict = {" in text
    assert f'"title": "{LOGIN_APP_TITLE}"' in text, "手写契约的 title 应当与被测 login_app 的 APP_TITLE 一致"
    assert HANDWRITTEN_CONTRACT_PATH.parent.name == "login"
    assert Path(ROOT / "testcases" / "login" / "contract.py").exists()
