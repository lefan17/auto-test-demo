"""UI 测试专用 conftest：浏览器生命周期 fixture + 失败自动截图。

作用域选择（面试高频考点，也是踩过的坑）：
- browser 用 session：启动浏览器很贵（1-3 秒），整个会话只启动一次；
- context 必须用 function：浏览器上下文是 cookie / localStorage 的隔离边界。
  saucedemo 的购物车就存在 localStorage 里——如果 context 共用，
  上一条用例加的商品会串到下一条，表现为「加了 1 个，角标显示 2」这种假失败。
  创建 context 的成本远低于启动浏览器（几十毫秒），完全值得。
- page 用 function：每条用例一个干净标签页。
"""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ui import InventoryPage, LoginPage

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"


# ---------- 浏览器生命周期 ----------
@pytest.fixture(scope="session")
def browser():
    """session 级浏览器：一次启动，全部 UI 用例共用（启动最贵，复用收益最大）。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # 想看界面就改成 headless=False
        yield browser
        browser.close()


@pytest.fixture
def browser_context(browser):
    """function 级上下文：每条用例独立的 cookie / localStorage。

    这是用例隔离的关键。用 session 级 context 会「快」但串状态，
    串状态导致的假失败比多花 0.1 秒创建 context 的代价大得多。
    """
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    yield context
    context.close()


@pytest.fixture
def page(browser_context):
    """function 级页面：每条用例一个独立标签页。"""
    page = browser_context.new_page()
    yield page
    page.close()


# ---------- 页面对象 fixture：用例直接拿页面，不自己 new ----------
@pytest.fixture
def login_page(page, ui_base_url) -> LoginPage:
    return LoginPage(page, ui_base_url).open()


@pytest.fixture
def inventory_page(page, ui_base_url) -> InventoryPage:
    return InventoryPage(page, ui_base_url)


@pytest.fixture
def logged_in_page(page, ui_base_url, config) -> InventoryPage:
    """已登录状态：把「登录」这个前置步骤做成 fixture，用例只关注业务本身。

    这就是 fixture 的价值 —— 前置条件复用，而不是每条用例复制粘贴登录代码。
    """
    user = config["user"]["valid"]
    LoginPage(page, ui_base_url).open().login(user["username"], user["password"])
    inventory = InventoryPage(page, ui_base_url)
    return inventory.wait_loaded()


# ---------- 失败自动截图 ----------
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """用例断言失败时自动截图存档，方便定位问题。

    原理：本钩子在 setup/call/teardown 三个阶段各触发一次，断言失败发生在 call 阶段；
    通过 item.funcargs 拿到用例正在用的 page，把当时的界面截下来。
    """
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or not report.failed:
        return

    page = item.funcargs.get("page")
    if page is None:
        return

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = ARTIFACT_DIR / f"{item.name}.png"
    try:
        page.screenshot(path=str(screenshot), full_page=True)
        print(f"\n[失败截图] {screenshot}")
    except Exception as e:  # noqa: BLE001 - 截图失败不能掩盖原始报错
        print(f"\n[截图失败] {e}")
