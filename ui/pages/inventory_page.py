"""商品列表页 Page Object。"""

import allure
from playwright.sync_api import Page


class InventoryPage:
    URL = "/inventory.html"

    TITLE = ".title"
    ITEM_NAMES = ".inventory_item_name"
    ADD_TO_CART_BUTTONS = "button[data-test^='add-to-cart']"
    CART_BADGE = ".shopping_cart_badge"
    CART_LINK = ".shopping_cart_link"
    MENU_BUTTON = "#react-burger-menu-btn"
    LOGOUT_LINK = "#logout_sidebar_link"

    def __init__(self, page: Page, base_url: str = ""):
        self.page = page
        self.base_url = base_url.rstrip("/")

    @allure.step("等待商品列表加载完成")
    def wait_loaded(self) -> "InventoryPage":
        # 显式等待：等业务元素出现，而不是 page.wait_for_timeout(3000)
        self.page.wait_for_selector(self.ITEM_NAMES)
        return self

    def get_title(self) -> str:
        return self.page.inner_text(self.TITLE)

    def get_item_names(self) -> list[str]:
        return self.page.locator(self.ITEM_NAMES).all_inner_texts()

    def get_item_count(self) -> int:
        return self.page.locator(self.ITEM_NAMES).count()

    @allure.step("把第 {index} 个商品加入购物车")
    def add_to_cart(self, index: int = 0) -> "InventoryPage":
        self.page.locator(self.ADD_TO_CART_BUTTONS).nth(index).click()
        return self

    def get_cart_count(self) -> int:
        """购物车角标数量；没有角标时返回 0（元素不存在是正常状态）。"""
        badge = self.page.locator(self.CART_BADGE)
        if badge.count() == 0:
            return 0
        return int(badge.inner_text())

    @allure.step("进入购物车")
    def go_to_cart(self) -> "InventoryPage":
        self.page.click(self.CART_LINK)
        self.page.wait_for_selector(".cart_item")
        return self

    def get_cart_item_names(self) -> list[str]:
        return self.page.locator(".inventory_item_name").all_inner_texts()

    @allure.step("退出登录")
    def logout(self) -> None:
        self.page.click(self.MENU_BUTTON)
        self.page.click(self.LOGOUT_LINK)
        self.page.wait_for_selector("#login-button")
