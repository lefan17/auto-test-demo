import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.ui, pytest.mark.smoke]


@pytest.mark.smoke
def test_login_success_should_enter_inventory(login_page, page, inventory_page, config):
    """正向场景：正确账号密码应跳转到商品列表页。"""
    user = config["user"]["valid"]
    login_page.login(user["username"], user["password"])

    # expect 是 Playwright 的「自动等待断言」，会轮询直到超时，比手写 sleep 稳
    expect(page).to_have_url(f"{inventory_page.base_url}/inventory.html")
    assert inventory_page.get_title() == "Products"


def _ui_accounts(config) -> dict:
    """UI 账号从配置取，不在用例里写明文。

    为什么：这三个账号的口令在版本库里出现一次就是一次泄漏面。
    配置里写的是 `${UI_PASSWORD}`，由 common/config.py 读取时展开成实际口令，
    所以这里拿到的是能直接用的字符串，而仓库里没有任何明文。
    """
    return config["user"]


@pytest.mark.parametrize(
    ("account_key", "expected_keyword"),
    [
        ("locked", "locked out"),
        ("wrong_username", "do not match"),
        ("invalid_password", "do not match"),
        ("empty", "Username is required"),
    ],
    ids=["locked-user", "wrong-username", "wrong-password", "empty-account"],
)
def test_login_failed_should_show_error(login_page, config, account_key, expected_keyword):
    """异常场景参数化：一个方法覆盖 4 种失败情况。

    注意空账号的提示文案和其他不同（Username is required），
    这属于「边界值 + 提示文案校验」，是真实项目里容易漏测的点。

    参数化的维度从"用户名/口令字面量"改成了"用哪一组账号"：
    口令不再出现在用例文件里，而"哪一种失败"这个语义反而更清楚了。
    """
    accounts = _ui_accounts(config)
    if account_key == "wrong_username":
        # 用户名错、口令正确：验证的是"不区分用户名错还是口令错"这个安全口径
        username, password = accounts["invalid"]["username"], accounts["valid"]["password"]
    elif account_key == "invalid_password":
        username, password = accounts["valid"]["username"], accounts["invalid"]["password"]
    elif account_key == "empty":
        username, password = "", ""
    else:
        username, password = accounts["locked"]["username"], accounts["locked"]["password"]

    login_page.login(username, password)

    error = login_page.get_error_message()
    assert expected_keyword.lower() in error.lower(), f"提示文案不符: {error!r}"


def test_logout_should_return_to_login_page(logged_in_page, page):
    """退出登录：应回到登录页，且登录按钮可见。"""
    logged_in_page.logout()

    expect(page.locator("#login-button")).to_be_visible()
    expect(page).not_to_have_url(f"{logged_in_page.base_url}/inventory.html")
