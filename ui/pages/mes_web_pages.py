"""Demo Web 的页面对象：登录页 + 工单列表页。

契约（`FROZEN_MESWEB.md` §5）冻结了 `data-test` 属性名，两侧（Demo Web 与用例）各自独立依赖它，
所以定位器集中写在这里，改名只改这一个文件——这正是页面对象存在的理由。

一个关键约束来自契约 §5「行内定位约定」：`[data-test="order-no"]` 的文本**严格等于** `order_no`，
所以「按单号找行」用**带锚点的正则**精确匹配，而不是子串匹配。子串匹配在单号互为前缀时会静默串行
（`WO-1` 会匹配到 `WO-10`），这种错最难查。
"""

from __future__ import annotations

import re

import allure
from playwright.sync_api import Locator, Page


class MesLoginPage:
    """登录页。"""

    URL = "/login"
    USERNAME_INPUT = '[data-test="username"]'
    PASSWORD_INPUT = '[data-test="password"]'
    LOGIN_BUTTON = '[data-test="login-button"]'
    ERROR_MESSAGE = '[data-test="error"]'

    def __init__(self, page: Page, base_url: str = "") -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")

    @allure.step("打开 Demo Web 登录页")
    def open(self) -> "MesLoginPage":
        self.page.goto(f"{self.base_url}{self.URL}")
        return self

    @allure.step("输入用户名")
    def input_username(self, username: str) -> "MesLoginPage":
        self.page.fill(self.USERNAME_INPUT, username)
        return self

    @allure.step("输入口令")
    def input_password(self, password: str) -> "MesLoginPage":
        self.page.fill(self.PASSWORD_INPUT, password)
        return self

    @allure.step("点击登录")
    def click_login(self) -> "MesLoginPage":
        self.page.click(self.LOGIN_BUTTON)
        return self

    @allure.step("登录：{username}")
    def login(self, username: str, password: str) -> "MesLoginPage":
        self.input_username(username)
        self.input_password(password)
        self.click_login()
        return self

    def error_locator(self) -> Locator:
        return self.page.locator(self.ERROR_MESSAGE)

    @allure.step("读取登录失败提示")
    def get_error_message(self) -> str:
        return self.error_locator().inner_text()

    def has_error(self) -> bool:
        """失败提示是否渲染。成功登录后契约要求此元素**不出现**，所以这是可反证的断言点。"""
        return self.error_locator().count() > 0


class WorkOrdersPage:
    """工单列表页。"""

    URL = "/work-orders"
    PAGE_TITLE = '[data-test="page-title"]'
    ROWS = '[data-test="work-order-row"]'
    ORDER_NO = '[data-test="order-no"]'
    PRODUCT_NAME = '[data-test="product-name"]'
    STATUS = '[data-test="status"]'
    APPROVE_BUTTON = '[data-test="approve-button"]'
    LOGOUT_BUTTON = '[data-test="logout-button"]'

    def __init__(self, page: Page, base_url: str = "") -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")

    @allure.step("打开工单列表页")
    def open(self) -> "WorkOrdersPage":
        self.page.goto(f"{self.base_url}{self.URL}")
        return self

    @allure.step("等待工单列表页渲染完成")
    def wait_loaded(self) -> "WorkOrdersPage":
        self.page.wait_for_selector(self.PAGE_TITLE)
        return self

    def get_page_title(self) -> str:
        return self.page.locator(self.PAGE_TITLE).inner_text()

    def rows(self) -> Locator:
        return self.page.locator(self.ROWS)

    def row_count(self) -> int:
        return self.rows().count()

    @allure.step("按单号定位行：{order_no}")
    def row_by_order_no(self, order_no: str) -> Locator:
        """先按单号精确找到那一行，再在该行内定位子元素。

        用锚点正则而不是 `get_by_text` 的默认子串行为：单号互为前缀时子串匹配会挑错行。
        """
        cell = self.page.locator(self.ORDER_NO, has_text=re.compile(rf"^{re.escape(order_no)}$"))
        return self.page.locator(self.ROWS).filter(has=cell)

    def order_numbers(self) -> list[str]:
        return [text.strip() for text in self.page.locator(self.ORDER_NO).all_inner_texts()]

    def has_order(self, order_no: str) -> bool:
        return self.row_by_order_no(order_no).count() > 0

    def status_of(self, order_no: str) -> str:
        """读某一行的状态值（契约 §5：`[data-test="status"]` 的值是 `status` 本身）。"""
        return self.row_by_order_no(order_no).locator(self.STATUS).inner_text().strip()

    def product_name_of(self, order_no: str) -> str:
        return self.row_by_order_no(order_no).locator(self.PRODUCT_NAME).inner_text().strip()

    def approve_button_of(self, order_no: str) -> Locator:
        """行内审批按钮。非 `PENDING` 时契约要求它**不渲染**，所以调用方可以断言 count()==0。"""
        return self.row_by_order_no(order_no).locator(self.APPROVE_BUTTON)

    @allure.step("在行内点击审批：{order_no}")
    def click_approve(self, order_no: str) -> "WorkOrdersPage":
        self.approve_button_of(order_no).click()
        return self

    @allure.step("点击退出登录")
    def click_logout(self) -> "MesLoginPage":
        self.page.click(self.LOGOUT_BUTTON)
        return MesLoginPage(self.page, self.base_url)
