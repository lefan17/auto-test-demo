import pytest

from ui.pages.login_page import LoginPage

pytestmark = [pytest.mark.ui, pytest.mark.regression]


def test_inventory_should_list_products(logged_in_page):
    """商品列表：默认展示 6 个商品，且名称非空。"""
    names = logged_in_page.get_item_names()

    assert logged_in_page.get_item_count() == 6, f"商品数量异常: {logged_in_page.get_item_count()}"
    assert all(name.strip() for name in names), f"存在空商品名: {names}"


def test_add_single_item_to_cart(logged_in_page):
    """加入购物车：角标数量应变为 1。"""
    logged_in_page.add_to_cart(0)

    assert logged_in_page.get_cart_count() == 1, "购物车角标未更新"


@pytest.mark.parametrize("count", [1, 2, 3], ids=lambda v: f"add-{v}-items")
def test_add_multiple_items_to_cart(logged_in_page, count):
    """参数化：加 1/2/3 个商品，角标数量应同步。"""
    for i in range(count):
        logged_in_page.add_to_cart(i)

    assert logged_in_page.get_cart_count() == count, f"加了 {count} 个，角标显示 {logged_in_page.get_cart_count()}"


def test_cart_should_contain_added_item(logged_in_page):
    """核心链路：列表加购 -> 进购物车 -> 商品在购物车里。

    断言「加到购物车的商品」和「购物车里显示的商品」是同一个，
    只断言数量会漏掉加错商品的 bug。
    """
    expected_name = logged_in_page.get_item_names()[0]
    logged_in_page.add_to_cart(0)

    logged_in_page.go_to_cart()

    cart_items = logged_in_page.get_cart_item_names()
    assert expected_name in cart_items, f"购物车中没有 {expected_name!r}，实际: {cart_items}"


def test_page_title_should_be_swag_labs(logged_in_page, page):
    """页面标题校验：最容易写的用例，但能快速发现环境/部署问题。"""
    assert page.title() == "Swag Labs", f"页面标题异常: {page.title()}"


def test_login_page_elements_should_be_visible(page, ui_base_url):
    """元素渲染检查：登录页三个核心元素必须可见。"""
    LoginPage(page, ui_base_url).open()

    assert page.locator("#user-name").is_visible(), "用户名输入框不可见"
    assert page.locator("#password").is_visible(), "密码输入框不可见"
    assert page.locator("#login-button").is_visible(), "登录按钮不可见"
