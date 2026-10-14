"""契约驱动测试模块的本地 fixture。

本目录要起真服务、要读入库的 spec，因此这里提供三样东西：
1. session 级 `MockServer`（复用 testcases/mes/mock_server_ctl.py，不重写第二份启动逻辑）；
2. 纯净视图与补全视图两份 spec（区别见 spec_contract.py 的模块 docstring）；
3. 各角色令牌（从 mock_server/db.py 的 SEED_USERS 取口令，不碰 apis/）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CONTRACT_DIR.parent.parent
MES_DIR = ROOT_DIR / "testcases" / "mes"
LOGIN_DIR = ROOT_DIR / "testcases" / "login"
for _p in (ROOT_DIR, MES_DIR, LOGIN_DIR, CONTRACT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from login_server_ctl import LoginServer  # noqa: E402
from mock_server_ctl import MockServer  # noqa: E402
from spec_contract import (  # noqa: E402
    PRISTINE_SPEC_PATH,
    SEED_CREDENTIALS,
    build_test_view,
    load_spec,
)

# 属性测试规模：默认小到几秒内跑完，放大用环境变量。
# 之所以要有默认值而不是"让 hypothesis 自己定"：CI 里必须可控。
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


@pytest.fixture(scope="session")
def contract_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MockServer]:
    tmp = tmp_path_factory.mktemp("contract")
    server = MockServer(db_path=tmp / "contract.db", log_path=tmp / "contract-server.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def contract_base_url(contract_server: MockServer) -> str:
    """形如 http://127.0.0.1:<port>/api/v1 —— spec 里的 path 已含 /api/v1 前缀，
    因此 schemathesis 的 base_url 只到 origin。"""
    return contract_server.root_url


@pytest.fixture(scope="session")
def pristine_spec() -> dict:
    """入库的、未经任何测试期修改的 spec。"""
    return load_spec(PRISTINE_SPEC_PATH)


@pytest.fixture(scope="session")
def test_view(pristine_spec: dict) -> dict:
    """只给 schemathesis 用的补全视图（不写盘）。"""
    return build_test_view(pristine_spec)


@pytest.fixture(scope="session")
def tokens(contract_server: MockServer) -> dict[str, str]:
    """角色 -> 令牌。口令取自 mock_server/db.py:177-184 的 SEED_USERS。"""
    import requests

    result: dict[str, str] = {}
    for role, (username, password) in SEED_CREDENTIALS.items():
        resp = requests.post(
            f"{contract_server.base_url}/auth/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        assert resp.status_code == 200, f"{role} 登录失败：{resp.status_code} {resp.text}"
        result[role] = resp.json()["access_token"]
    return result


@pytest.fixture(scope="session")
def admin_token(tokens: dict[str, str]) -> str:
    return tokens["ADMIN"]


@pytest.fixture(scope="session")
def login_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LoginServer]:
    """login_app 服务：手写契约的对照物（testcases/login/contract.py 描述的就是它）。"""
    tmp = tmp_path_factory.mktemp("contract-login")
    server = LoginServer(log_path=tmp / "login-server.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()
