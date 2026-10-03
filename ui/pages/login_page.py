"""UI Page Object 层：页面元素和操作写在页面类里，用例只调方法。

PO 模式的核心价值（面试必问）：
1. 页面改了（比如 id 从 user-name 改成 username），只改这个类，用例不动；
2. 用例读起来是业务语言（login()、add_to_cart()），不是一堆 selector；
3. 元素定位方式集中，方便统一改成 data-testid 等稳定方案。
"""

import allure
from playwright.sync_api import Page


class LoginPage:
    URL = "/"

    # 元素定位器统一写在类顶部，便于维护；优先用 data-test 属性，比 XPath 稳
    USERNAME_INPUT = "#user-name"
    PASSWORD_INPUT = "#password"
    LOGIN_BUTTON = "#login-button"
    ERROR_MESSAGE = "[data-test='error']"

    def __init__(self, page: Page, base_url: str = ""):
        self.page = page
        self.base_url = base_url.rstrip("/")

    @allure.step("打开登录页")
    def open(self) -> "LoginPage":
        self.page.goto(f"{self.base_url}{self.URL}")
        return self

    @allure.step("输入用户名: {username}")
    def input_username(self, username: str) -> "LoginPage":
        self.page.fill(self.USERNAME_INPUT, username)
        return self

    @allure.step("输入密码")
    def input_password(self, password: str) -> "LoginPage":
        self.page.fill(self.PASSWORD_INPUT, password)
        return self

    @allure.step("点击登录")
    def click_login(self) -> "LoginPage":
        self.page.click(self.LOGIN_BUTTON)
        return self

    def login(self, username: str, password: str) -> "LoginPage":
        """组合操作：登录三步合成一个业务动作。"""
        return self.input_username(username).input_password(password).click_login()

    def get_error_message(self) -> str:
        """获取错误提示文案（等待元素出现，不写死 sleep）。"""
        self.page.wait_for_selector(self.ERROR_MESSAGE, state="visible")
        return self.page.inner_text(self.ERROR_MESSAGE)
