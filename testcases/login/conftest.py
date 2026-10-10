"""登录服务的专用 fixture。

放在 testcases/login/ 下的原因（与 testcases/mes/ 同理，不污染根 conftest）：
根 conftest 管的是公网练习站那套（jsonplaceholder / saucedemo），
本目录要起本地服务。两者生命周期完全不同，混在一起会让 `pytest -m api`
也被迫加载 uvicorn 相关的东西。
"""

from __future__ import annotations

import pytest
import requests
from login_server_ctl import LoginServer


# ---------- 服务：整个测试会话起一次 ----------
@pytest.fixture(scope="session")
def login_server(tmp_path_factory) -> LoginServer:
    """session 级起一次被测登录服务。

    端口由系统分配（避免残留进程占端口），日志写进临时目录
    （失败时能翻，但不污染工作区）。
    """
    tmp_dir = tmp_path_factory.mktemp("login")
    server = LoginServer(log_path=tmp_dir / "login_server.log")
    server.start()
    try:
        yield server
    finally:
        # 无论成败都要停：CI 上留下僵尸 uvicorn 会让后续 job 卡在端口占用上
        server.stop()


@pytest.fixture(scope="session")
def login_base_url(login_server) -> str:
    return login_server.base_url


# ---------- 用例级会话：每条用例一个独立 Session ----------
@pytest.fixture
def login_api(login_base_url) -> "LoginClient":
    """每条用例一个干净的会话。

    为什么不用 session 级：登录接口是无状态的，用 session 级只会让
    "上一条用例改了 header" 之类的问题偷偷串到下一条。用例级成本可以忽略。
    """
    with LoginClient(login_base_url) as client:
        yield client


class LoginClient:
    """登录服务的最小客户端：只做"发请求"，不做断言。

    分层原则与本项目 apis/ 一致：**请求与断言分开**。
    这里没有单独建 apis/login_api.py，是因为契约只有一个接口，
    为它单独造一个业务封装层属于过度设计；接口一旦变多就该抽出去。
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ---------- 请求方法 ----------
    def login_json(self, payload: dict | None = None) -> requests.Response:
        """JSON 方式登录（契约定义的方式）。"""
        return self.session.post(
            f"{self.base_url}/api/login", json=payload, timeout=self.timeout
        )

    def login_form(self, payload: dict | None = None) -> requests.Response:
        """表单方式登录（契约未定义，实现额外兼容 —— FIND-02）。"""
        return self.session.post(
            f"{self.base_url}/api/login", data=payload, timeout=self.timeout
        )

    def login_raw(self, body: str, content_type: str = "text/plain") -> requests.Response:
        """发送任意请求体，用于测"非 JSON 请求体"这类协议层场景。"""
        return self.session.post(
            f"{self.base_url}/api/login",
            data=body,
            headers={"Content-Type": content_type},
            timeout=self.timeout,
        )

    def health(self) -> requests.Response:
        return self.session.get(f"{self.base_url}/api/health", timeout=self.timeout)

    # ---------- 生命周期 ----------
    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "LoginClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
