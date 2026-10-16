"""混合链路用例：**接口造数 → UI 走流程 → 接口校验**（起浏览器）。

这条链路存在的理由：`base.ui` 那个外网练习站和本仓库自己的被测服务毫无业务关系，
在它上面「UI 操作」和「接口断言」操作的不是同一份数据，链路是断的。这里 Demo Web 从
`mock_server` 取数渲染，浏览器操作它，最后再回接口这一侧独立确认状态——
两个方向各用一次接口，中间的 UI 操作不来自证。

浏览器 fixture 复用 `testcases/ui/conftest.py` 的 `browser` / `browser_context` / `page`
以及失败截图钩子（契约 §6.3），**不另写一套**。

两个服务在这里自建 fixture：`testcases/ui/` 看不到 `testcases/mes/conftest.py` 的
`mes_server`（conftest 只对自己的目录及以下生效），而且契约禁止改任何 conftest。
两个 import 形式不同也是实测结论（契约 §6.2），写错必然 `ModuleNotFoundError`。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from mock_server_ctl import MockServer
from playwright.sync_api import Page, expect

from apis import MesApi
from common.secrets import role_password, role_username
from testcases.ui.mes_web_ctl import MesWebServer
from ui.pages.mes_web_pages import MesLoginPage, WorkOrdersPage

pytestmark = [pytest.mark.ui, pytest.mark.regression]

APPROVER_ROLE = "PLANNER"


@pytest.fixture(scope="session")
def mixed_mock_server(tmp_path_factory) -> Iterator[MockServer]:
    tmp_dir = tmp_path_factory.mktemp("mes_mixed")
    server = MockServer(db_path=tmp_dir / "mes_mixed.db", log_path=tmp_dir / "mock_server.log")
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def mixed_mes_web(mixed_mock_server, tmp_path_factory) -> Iterator[MesWebServer]:
    # 必须传 root_url：传 MockServer.base_url（已含 /api/v1）会让转发变成
    # /api/v1/api/v1/... 全 404，而 _wait_healthy 的核对就是用来抓这种错配的。
    server = MesWebServer(
        mock_base_url=mixed_mock_server.root_url,
        log_path=Path(tmp_path_factory.mktemp("mes_mixed_srv")) / "mes_web.log",
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def api(mixed_mock_server) -> MesApi:
    return MesApi(mixed_mock_server.base_url)


@pytest.fixture
def login_page(page: Page, mixed_mes_web) -> MesLoginPage:
    return MesLoginPage(page, mixed_mes_web.base_url).open()


@pytest.fixture
def logged_in_orders(page: Page, mixed_mes_web) -> WorkOrdersPage:
    """前置「已登录」做成 fixture：用例只关注业务本身，不重复抄登录代码。

    这里刻意走**真实登录页**而不是往浏览器里塞 cookie：塞 cookie 会绕过被测的那段
    转发 + 会话代码，而这段代码正是本批次新写的、最需要被走一遍的部分。
    """
    MesLoginPage(page, mixed_mes_web.base_url).open().login(role_username(APPROVER_ROLE), role_password(APPROVER_ROLE))
    return WorkOrdersPage(page, mixed_mes_web.base_url).wait_loaded()


def _create_order(api: MesApi) -> tuple[str, str]:
    """经接口造一张 PENDING 工单（单号随机，不写死）。"""
    order_no = f"WO-MIX-{uuid.uuid4().hex[:10].upper()}"
    product_name = f"混合链路产品-{order_no}"
    resp = api.create_work_order(
        APPROVER_ROLE,
        order_no=order_no,
        product_code="FG-MIX-01",
        product_name=product_name,
        planned_qty="12.000",
    )
    MesApi.expect_ok(resp, status=201)
    return order_no, product_name


# --------------------------------------------------------------------------
# 核心闭环
# --------------------------------------------------------------------------
@pytest.mark.covers("get_work_order_api_v1_work_orders__order_no__get")
def test_api_seed_ui_approve_api_verify_should_close_the_loop(page, api, logged_in_orders):
    """核心闭环：接口造数（PENDING）→ UI 审批 → 接口校验（APPROVED + version 自增）。

    为什么前置状态也要经接口确认：UI 上显示 PENDING 只能证明渲染对，不证明库里的状态是
    PENDING。如果造数那一步其实失败/建成了别的状态，UI 断言会跟着一起错，而这条用例
    依然可能"绿"——所以两端都用接口独立取证。
    """
    order_no, product_name = _create_order(api)

    before = api.work_order(order_no, role=APPROVER_ROLE)
    MesApi.expect_ok(before)
    before_body = before.json()
    assert before_body["status"] == "PENDING"
    # UI 走流程：先在列表里按单号定位到那一行，再在行内点击审批
    orders = logged_in_orders
    orders.open().wait_loaded()
    row = orders.row_by_order_no(order_no)
    expect(row).to_have_count(1)
    expect(row.locator(WorkOrdersPage.STATUS)).to_have_text("PENDING")
    expect(row.locator(WorkOrdersPage.PRODUCT_NAME)).to_have_text(product_name)
    orders.click_approve(order_no)

    # 审批成功 → 回到列表页，该行变成 APPROVED 且不再提供审批按钮
    orders.wait_loaded()
    expect(page).to_have_url(f"{orders.base_url}{WorkOrdersPage.URL}")
    approved_row = orders.row_by_order_no(order_no)
    expect(approved_row.locator(WorkOrdersPage.STATUS)).to_have_text("APPROVED")
    expect(approved_row.locator(WorkOrdersPage.APPROVE_BUTTON)).to_have_count(0)

    # 接口校验：回到接口这一侧独立确认，不采信 UI 自己的渲染
    after = api.work_order(order_no, role=APPROVER_ROLE)
    MesApi.expect_ok(after)
    after_body = after.json()
    assert after_body["status"] == "APPROVED"
    assert after_body["version"] == before_body["version"] + 1


@pytest.mark.covers("get_work_order_api_v1_work_orders__order_no__get")
def test_approve_should_only_affect_the_targeted_row(page, api, logged_in_orders):
    """行内定位必须精确：审批 A 单不能动到 B 单。

    页面对象用锚点正则按单号找行，就是为了这条。如果退化成子串匹配，
    单号互为前缀时（`WO-X-1` / `WO-X-10`）会点到错的行——这是本批次里最容易静默错位的一处。
    """
    target_no, _ = _create_order(api)
    other_no, _ = _create_order(api)

    orders = logged_in_orders
    orders.open().wait_loaded()
    assert orders.row_by_order_no(target_no).count() == 1
    assert orders.row_by_order_no(other_no).count() == 1
    orders.click_approve(target_no)
    orders.wait_loaded()

    target = api.work_order(target_no, role=APPROVER_ROLE).json()
    other = api.work_order(other_no, role=APPROVER_ROLE).json()
    assert target["status"] == "APPROVED"
    assert other["status"] == "PENDING"


# --------------------------------------------------------------------------
# 服务端渲染与页面的其他契约点
# --------------------------------------------------------------------------
def test_login_with_wrong_password_should_show_page_error(page, mixed_mes_web):
    """真实浏览器里的登录失败路径：页面出现错误提示，且**仍在登录页**（没有会话）。"""
    MesLoginPage(page, mixed_mes_web.base_url).open().login(role_username(APPROVER_ROLE), "definitely-not-the-password")

    error = MesLoginPage(page).error_locator()
    expect(error).to_be_visible()
    expect(page).to_have_url(f"{mixed_mes_web.base_url}{MesLoginPage.URL}")


def test_anonymous_visit_should_land_on_login(page, mixed_mes_web):
    """未登录直接访问受保护页：浏览器最终停在登录页（302→/login 被自动跟随）。"""
    page.goto(f"{mixed_mes_web.base_url}{WorkOrdersPage.URL}")

    expect(page).to_have_url(f"{mixed_mes_web.base_url}{MesLoginPage.URL}")
    expect(page.locator(MesLoginPage.LOGIN_BUTTON)).to_be_visible()


def test_logout_should_remove_session_and_hide_orders(logged_in_orders, page, api):
    """退出登录后会话失效：再访问列表页会被弹回登录页。

    只断言"跳到了登录页"是不够的：不删服务端会话、只删 cookie 也能让当前这次跳转发生，
    但重新带上旧 cookie 就又能进——所以这里退出后**再访问一次**受保护页来验会话真的没了。
    """
    order_no, _ = _create_order(api)
    orders = logged_in_orders
    orders.open().wait_loaded()
    expect(orders.row_by_order_no(order_no)).to_have_count(1)

    orders.click_logout()
    expect(page).to_have_url(f"{orders.base_url}{MesLoginPage.URL}")

    page.goto(f"{orders.base_url}{WorkOrdersPage.URL}")
    expect(page).to_have_url(f"{orders.base_url}{MesLoginPage.URL}")
