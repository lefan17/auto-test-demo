"""全局 fixture：所有测试都能用，不需要 import。

pytest 的 conftest.py 是按目录层级自动生效的 —— 放在项目根目录，
下面的 testcases/ 全部继承。
"""

import sys
from pathlib import Path

import pytest

# 把项目根目录加进 sys.path，否则 testcases/ 里 `from apis import UserApi` 会报
# ModuleNotFoundError。加了 pytest.ini 的 rootdir 后通常也会带上，但显式写更稳。
# 同时把 testcases/ 也加进去：testcases/mes/ 里的用例要 import 同目录的
# mock_server_ctl / data_factory，pytest 的 rootdir 插入策略在不同调用方式下
# （pytest / python -m pytest）行为不一致，显式插入最省事。
#
# 顺序很关键：必须**先**把 ROOT_DIR 插到最前面，再处理其它路径。
# 源码包叫 apis、测试目录叫 testcases/api，两者不同名，就是为了避免
# `testcases/api/__init__.py` 把 `api` 这个名字抢走（见 apis/__init__.py 的说明）。
ROOT_DIR = Path(__file__).resolve().parent
TESTCASES_DIR = ROOT_DIR / "testcases"
for _p in (ROOT_DIR, TESTCASES_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from apis import PostApi, UserApi  # noqa: E402
from common.yaml_util import get_config  # noqa: E402


# ---------- session 级配置：整个测试会话只读一次文件 ----------
@pytest.fixture(scope="session")
def config() -> dict:
    return get_config()


@pytest.fixture(scope="session")
def api_base_url(config) -> str:
    return config["base"]["api"]


@pytest.fixture(scope="session")
def ui_base_url(config) -> str:
    return config["base"]["ui"]


# ---------- function 级接口对象：每条用例一个干净 Session，用例之间互不污染 ----------
# 超时不从这里传：传输层参数由 BaseApi 统一管（环境变量 API_TIMEOUT / API_RETRIES），
# 免得同一个超时值在 YAML 和代码里各存一份，改一处忘一处。
@pytest.fixture
def user_api(api_base_url) -> UserApi:
    return UserApi(api_base_url)


@pytest.fixture
def post_api(api_base_url) -> PostApi:
    return PostApi(api_base_url)


# ---------- 报告相关钩子：把被测环境地址写进 Allure 报告的环境信息页 ----------
def pytest_configure(config):
    try:
        cfg = get_config()
        env_info = {
            "API地址": cfg["base"]["api"],
            "UI地址": cfg["base"]["ui"],
            "Python版本": sys.version.split()[0],
        }
        # 只在带 --alluredir 参数运行时才写；普通 pytest 运行没有这个属性
        if hasattr(config, "allure_environment"):
            config.allure_environment.extend(f"{k}={v}" for k, v in env_info.items())
    except Exception:  # noqa: BLE001 - 报告信息绝不能影响测试本身
        pass


def pytest_html_report_title(report):
    report.title = "自动化测试报告"
