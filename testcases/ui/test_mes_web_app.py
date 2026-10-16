"""Demo Web 自身的 HTTP 层用例：**不申请 `page`，因此不会启动 chromium**（契约 §6.3）。

这里直接用 `requests` 打 `mes_web` 的 HTTP 接口，验的是「服务端行为」：重定向码、`Set-Cookie`、
服务端渲染出来的 DOM 是否满足契约 §5。浏览器那一层由 `test_mes_mixed_flow.py` 负责。

为什么要有这一层：混合链路用例一旦变红，可能是「Web 的 HTTP 行为坏了」也可能是「浏览器交互坏了」。
两条分开跑，读一次报错就知道该看哪边；而且这一层没有浏览器，跑起来是秒级、可在 CI 里当快速反馈。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
from mock_server_ctl import MockServer

from apis import MesApi
from common.secrets import role_password, role_username
from testcases.ui.mes_web_ctl import MesWebServer

pytestmark = [pytest.mark.ui, pytest.mark.regression]

# 审批是 PLANNER / ADMIN 的权限（契约 §4），混合链路统一用 PLANNER。
APPROVER_ROLE = "PLANNER"
COOKIE_PREFIX = "mes_web_session"


# --------------------------------------------------------------------------
# 两个服务：session 级起一次（会话结束无论成败都停，避免留僵尸进程占端口）
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def http_mock_server(tmp_path_factory) -> Iterator[MockServer]:
    tmp_dir = tmp_path_factory.mktemp("mes_web_http")
    server = MockServer(db_path=tmp_dir / "mes_web_http.db", log_path=tmp_dir / "mock_server.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def http_mes_web(http_mock_server, tmp_path_factory) -> Iterator[MesWebServer]:
    # 传 root_url（不含 /api/v1），让 mes_web 自己补前缀——传 base_url 会让转发全 404。
    server = MesWebServer(
        mock_base_url=http_mock_server.root_url,
        log_path=Path(tmp_path_factory.mktemp("mes_web_http_srv")) / "mes_web.log",
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def http_api(http_mock_server) -> MesApi:
    return MesApi(http_mock_server.base_url)


@pytest.fixture
def web(http_mes_web) -> str:
    return http_mes_web.base_url


def _new_order(api: MesApi) -> tuple[str, str]:
    """经接口造一张 PENDING 工单，返回 (order_no, product_name)。

    单号由 `uuid4` 生成，不写死：写死的单号会在第二次跑（或与别人并行跑）时撞上已存在的单，
    到时看到的报错是「创建冲突」，很容易误判成代码坏了。
    """
    order_no = f"WO-HTTP-{uuid.uuid4().hex[:10].upper()}"
    product_name = f"HTTP层产品-{order_no}"
    resp = api.create_work_order(
        APPROVER_ROLE,
        order_no=order_no,
        product_code="FG-HTTP-01",
        product_name=product_name,
        planned_qty="5.000",
    )
    MesApi.expect_ok(resp, status=201)
    return order_no, product_name


def _login(client: requests.Session, base_url: str) -> requests.Response:
    return client.post(
        f"{base_url}/login",
        data={
            "username": role_username(APPROVER_ROLE),
            "password": role_password(APPROVER_ROLE),
        },
        allow_redirects=False,
    )


# --------------------------------------------------------------------------
# 探针与根路由
# --------------------------------------------------------------------------
def test_healthz_should_echo_mock_base_url(web, http_mock_server):
    """`/healthz` 必须原样回显收到的根 URL——`_wait_healthy` 就是靠这个抓「转发到了错后端」。"""
    resp = requests.get(f"{web}/healthz", timeout=10)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mock_base_url"] == http_mock_server.root_url


def test_root_should_redirect_anonymous_to_login(web):
    resp = requests.get(f"{web}/", allow_redirects=False, timeout=10)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_root_should_redirect_logged_in_to_work_orders(web):
    with requests.Session() as client:
        _login(client, web)
        resp = client.get(f"{web}/", allow_redirects=False, timeout=10)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/work-orders"


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------
def test_login_page_should_render_dom_contract_without_error(web):
    """登录页服务端渲染出的 DOM 必须满足契约 §5，且初始状态**没有**失败提示。"""
    resp = requests.get(f"{web}/login", timeout=10)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert 'data-test="username"' in body
    assert 'name="username"' in body
    assert 'data-test="password"' in body
    assert 'name="password"' in body
    assert 'data-test="login-button"' in body
    assert 'type="submit"' in body
    assert 'data-test="error"' not in body


def test_login_with_wrong_password_should_rerender_error_without_cookie(web):
    """失败路径：200 重渲染 + 页面里有 `[data-test="error"]`，且**不能**下发会话 cookie。"""
    resp = requests.post(
        f"{web}/login",
        data={"username": role_username(APPROVER_ROLE), "password": "definitely-not-the-password"},
        allow_redirects=False,
        timeout=10,
    )

    assert resp.status_code == 200
    assert 'data-test="error"' in resp.text
    assert COOKIE_PREFIX not in resp.headers.get("set-cookie", "")


def test_login_with_password_not_in_plain_cookie(web):
    """会话 cookie 是不透明随机串：不能把口令/用户名编码进去（契约 §3.2）。

    这条单独立出来，是因为「cookie 里放用户名」这种实现看起来也能跑通全部功能用例，
    但把账号信息泄漏到了浏览器可读位置。它是个可独立证伪的约束，值得单独一条。
    """
    with requests.Session() as client:
        _login(client, web)
        cookie = client.cookies.get(COOKIE_PREFIX)

    assert cookie is not None
    assert role_username(APPROVER_ROLE) not in cookie
    assert role_password(APPROVER_ROLE) not in cookie


def test_login_with_missing_fields_should_not_crash(web):
    """表单字段缺失是真实会发生的（用户直接提交空表单），必须落在「重渲染错误」而不是 500。

    契约 §3.3 要求自己解析 body，这里就是在验那段解析对缺字段的处理。
    """
    resp = requests.post(f"{web}/login", data={}, allow_redirects=False, timeout=10)

    assert resp.status_code == 200
    assert 'data-test="error"' in resp.text


def test_login_success_should_set_cookie_and_redirect(web):
    """成功路径：303 → /work-orders，且下发不透明 cookie。"""
    with requests.Session() as client:
        resp = _login(client, web)
        cookie = client.cookies.get(COOKIE_PREFIX)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/work-orders"
    assert cookie is not None
    # 不透明随机串（`secrets.token_urlsafe(32)` 约 43 字符），不是用户名也不是令牌本身
    assert len(cookie) >= 32


def test_logout_should_clear_session(web):
    with requests.Session() as client:
        _login(client, web)
        assert client.get(f"{web}/work-orders", allow_redirects=False, timeout=10).status_code == 200

        resp = client.get(f"{web}/logout", allow_redirects=False, timeout=10)
        after = client.get(f"{web}/work-orders", allow_redirects=False, timeout=10)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # 退出后再访问受保护页必须重新跳登录页——只删 cookie 而不清服务端会话的话这里会漏
    assert after.status_code == 302
    assert after.headers["location"] == "/login"


# --------------------------------------------------------------------------
# 工单列表与审批（接口造数 → Web 渲染 → 接口校验）
# --------------------------------------------------------------------------
def test_work_orders_should_redirect_when_anonymous(web):
    resp = requests.get(f"{web}/work-orders", allow_redirects=False, timeout=10)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_work_orders_should_render_order_created_via_api(web, http_api):
    """接口造的工单必须出现在服务端渲染出来的 HTML 里，且 DOM 契约逐条满足。"""
    order_no, product_name = _new_order(http_api)

    with requests.Session() as client:
        _login(client, web)
        resp = client.get(f"{web}/work-orders", timeout=10)

    assert resp.status_code == 200
    body = resp.text
    assert 'data-test="page-title"' in body
    # 单号必须是纯文本单元格：容器内除了它自己不能有别的东西，
    # 否则页面对象的「按单号找行」会退化成子串匹配（契约 §5 行内定位约定）。
    assert f'<td data-test="order-no">{order_no}</td>' in body
    assert product_name in body
    assert 'data-test="work-order-row"' in body
    assert 'data-test="logout-button"' in body


@pytest.mark.covers("get_work_order_api_v1_work_orders__order_no__get")
def test_approve_should_transition_status_and_be_verifiable_via_api(web, http_api):
    """核心闭环：接口造数（PENDING）→ Web 审批 → 接口校验（APPROVED）。

    先经接口确认前置状态，再走 Web，再经接口确认后置状态，且 version 必须自增——
    只看 Web 的渲染结果会自证（同一份代码既写又验），必须回到接口这一侧独立确认。
    """
    order_no, _ = _new_order(http_api)
    before = http_api.work_order(order_no, role=APPROVER_ROLE)
    MesApi.expect_ok(before)
    before_body = before.json()
    assert before_body["status"] == "PENDING"

    with requests.Session() as client:
        _login(client, web)
        listing = client.get(f"{web}/work-orders", timeout=10)
        assert f'<td data-test="order-no">{order_no}</td>' in listing.text
        assert 'data-test="approve-button"' in listing.text

        resp = client.post(f"{web}/work-orders/{order_no}/approve", allow_redirects=False, timeout=10)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/work-orders"

    after = http_api.work_order(order_no, role=APPROVER_ROLE)
    MesApi.expect_ok(after)
    after_body = after.json()
    assert after_body["status"] == "APPROVED"
    assert after_body["version"] == before_body["version"] + 1


def test_approve_button_should_disappear_after_approval(web, http_api):
    """已审批的行不再渲染按钮（契约 §5），状态文本变成 APPROVED。

    这条与上一条验的不是同一件事：上一条验状态真的变了，这条验**页面不会再提供**那个动作——
    按钮留着的话用户会看到一个点了没反应的按钮，属于典型的界面与状态不同步。
    """
    order_no, _ = _new_order(http_api)

    with requests.Session() as client:
        _login(client, web)
        client.post(f"{web}/work-orders/{order_no}/approve", allow_redirects=False, timeout=10)
        resp = client.get(f"{web}/work-orders", timeout=10)

    assert resp.status_code == 200
    assert f'<td data-test="order-no">{order_no}</td>' in resp.text
    # 行内定位：以该单号为起点切出这一行，再在行内断言
    row = _row_html(resp.text, order_no)
    assert 'data-test="approve-button"' not in row
    assert '<td data-test="status">APPROVED</td>' in row


def test_approve_should_surface_upstream_error_instead_of_faking_success(web, http_api):
    """重复审批必须**暴露**上游错误，而不是假装成功。

    这是一条防御性的用例：如果 Demo Web 把上游非 200 也当成功、照样 303 回列表页，
    用户会以为自己审批成功了，实际状态没动。断言「不再返回 303 且页面上有上游错误原文」，
    才是对「转发层」这个唯一职责的有效约束。
    """
    order_no, _ = _new_order(http_api)

    with requests.Session() as client:
        _login(client, web)
        first = client.post(f"{web}/work-orders/{order_no}/approve", allow_redirects=False, timeout=10)
        second = client.post(f"{web}/work-orders/{order_no}/approve", allow_redirects=False, timeout=10)

    assert first.status_code == 303
    assert second.status_code == 502
    assert 'data-test="forward-error"' in second.text
    assert "mock_server 返回 HTTP" in second.text


def test_approve_unknown_order_no_should_surface_404(web):
    """单号不存在：上游 404 必须变成可诊断的 502 页面，不能吞掉。"""
    with requests.Session() as client:
        _login(client, web)
        resp = client.post(f"{web}/work-orders/WO-NOT-EXIST/approve", allow_redirects=False, timeout=10)

    assert resp.status_code == 502
    assert 'data-test="forward-error"' in resp.text


def _row_html(html: str, order_no: str) -> str:
    """从整页 HTML 里切出某一单号所在的那一行，供「行内断言」使用。"""
    marker = f'<td data-test="order-no">{order_no}</td>'
    end = html.index(marker) + len(marker)
    start = html.rindex('<tr data-test="work-order-row">', 0, end)
    return html[start : html.index("</tr>", end)]
