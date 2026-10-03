import pytest
from playwright.sync_api import expect

from ui import LoginPage

pytestmark = [pytest.mark.ui, pytest.mark.smoke]


@pytest.mark.smoke
def test_login_success_should_enter_inventory(login_page, page, inventory_page):
    """正向场景：正确账号密码应跳转到商品列表页。"""
    login_page.login("standard_user", "secret_sauce")

    # expect 是 Playwright 的「自动等待断言」，会轮询直到超时，比手写 sleep 稳
    expect(page).to_have_url(f"{inventory_page.base_url}/inventory.html")
    assert inventory_page.get_title() == "Products"


@pytest.mark.parametrize(
    "username, password, expected_keyword",
    [
        ("locked_out_user", "secret_sauce", "locked out"),
        ("wrong_user", "secret_sauce", "do not match"),
        ("standard_user", "wrong_password", "do not match"),
        ("", "", "Username is required"),
    ],
    ids=["locked-user", "wrong-username", "wrong-password", "empty-account"],
)
def test_login_failed_should_show_error(login_page, username, password, expected_keyword):
    """异常场景参数化：一个方法覆盖 4 种失败情况。

    注意空账号的提示文案和其他不同（Username is required），
    这属于「边界值 + 提示文案校验」，是真实项目里容易漏测的点。
    """
    login_page.login(username, password)

    error = login_page.get_error_message()
    assert expected_keyword.lower() in error.lower(), f"提示文案不符: {error!r}"


def test_logout_should_return_to_login_page(logged_in_page, page):
    """退出登录：应回到登录页，且登录按钮可见。"""
    logged_in_page.logout()

    expect(page.locator("#login-button")).to_be_visible()
    expect(page).not_to_have_url(f"{logged_in_page.base_url}/inventory.html")
