"""全局 fixture：所有测试都能用，不需要 import。

pytest 的 conftest.py 是按目录层级自动生效的 —— 放在项目根目录，
下面的 testcases/ 全部继承。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
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
# 子目录也要显式插：testcases/login/conftest.py 里 `from login_server_ctl import ...`
# 依赖它。只插 testcases/ 时，pytest 在 rootdir 模式下恰好会把子目录也带上，
# 但用 `python -m pytest` 或从别的目录调用时行为不同 —— 这正是 MES 那边
# 踩过的坑，两个子目录统一显式插入，不依赖隐式行为。
LOGIN_DIR = TESTCASES_DIR / "login"
MES_DIR = TESTCASES_DIR / "mes"
for _p in (ROOT_DIR, TESTCASES_DIR, LOGIN_DIR, MES_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from apis import PostApi, UserApi  # noqa: E402
from common.config import get_config  # noqa: E402


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


# ===========================================================================
# flaky 治理：重跑留痕 + 隔离区
# ===========================================================================
#
# 两件事都在这个文件里，因为它们都必须对**每一次** pytest 调用生效：
#   - 留痕：pytest-rerunfailures 自己不产出机器可读产物（16.7 不写 json/xml），
#     而 junit-xml 根本不认 outcome == "rerun"（venv 里 _pytest/junitxml.py 的
#     pytest_runtest_logreport 只处理 passed/failed/skipped），所以「重跑才通过」
#     这件事只能由我们自己记。
#   - 隔离区：quarantine 用例必须退出主门禁，但**必须打印清单**——
#     静默丢弃等于让用例库悄悄死亡，这正是本轮要治的病。
#
# 实测过的两个坑（决定了下面的写法）：
#   1. xdist 下 master 侧**不跑** pytest_collection_modifyitems（只有 worker 跑），
#      所以隔离清单要靠 config.workeroutput → node.workeroutput 桥回 master，
#      master 再统一打印 + 写 sidecar。worker 自己打印会丢。
#   2. master 侧的 pytest_runtest_logreport 能收到 worker 的 outcome == "rerun"
#      报告（report.rerun 字段也在），所以留痕钩子挂在 master 上就够。

_FLAKY_SIDECAR = Path(os.getenv("FLAKY_GOVERNANCE_LEDGER", str(ROOT_DIR / "reports" / "flaky-governance.json")))
_QUARANTINE_TOKEN_RE = re.compile(r"\bquarantine\b")

# 跨钩子累加的状态。
_quarantine_selected: list[str] = []
_rerun_seen: dict[str, int] = {}
_final_outcome: dict[str, str] = {}


def _is_quarantine_run(config) -> bool:
    """本次调用是否显式要跑隔离区（`-m quarantine` / `-m "quarantine or smoke"`）。

    用词边界而不是子串匹配：marker 名字里也可能含这个词，子串匹配会误判。
    """
    return _QUARANTINE_TOKEN_RE.search(config.getoption("markexpr", "") or "") is not None


def pytest_collection_modifyitems(config, items):
    """把 quarantine 用例移出主门禁（除非本次就是来跑它们的）。"""
    if _is_quarantine_run(config):
        return

    quarantined = [item for item in items if item.get_closest_marker("quarantine")]
    if not quarantined:
        return

    items[:] = [item for item in items if item.get_closest_marker("quarantine") is None]
    # 走标准钩子，让 pytest 的 "N deselected" 计数也把这一批算进去，
    # 而不是让它们从收集数里凭空消失。
    config.hook.pytest_deselected(items=quarantined)

    nodeids = sorted(item.nodeid for item in quarantined)
    if hasattr(config, "workerinput"):
        config.workeroutput.setdefault("quarantine_nodeids", []).extend(nodeids)
    else:
        _quarantine_selected.extend(nodeids)


def pytest_testnodedown(node, error):
    """xdist：把 worker 里被隔离的清单收到 master 上。"""
    nodeids = getattr(node, "workeroutput", {}).get("quarantine_nodeids")
    if nodeids:
        _quarantine_selected.extend(nodeids)


def pytest_runtest_logreport(report):
    """记录重跑事件。master 侧收得到，单进程时就是本进程。"""
    outcome = getattr(report, "outcome", None)
    if getattr(report, "when", None) != "call":
        return
    if outcome == "rerun":
        _rerun_seen[report.nodeid] = _rerun_seen.get(report.nodeid, 0) + 1
    elif outcome in ("passed", "failed"):
        _final_outcome[report.nodeid] = outcome


def _write_flaky_sidecar(session) -> None:
    """把「重跑才通过 / 重跑仍失败 / 被隔离」写成 JSON，供 scripts/flaky_report.py 消费。

    刻意不写进 junit-xml / Allure：那两个是**报告**，格式归各自的插件管；
    这里要的是一份稳定、可被脚本和周报直接读的**治理账本**。
    """
    rerun_passed = sorted(n for n, c in _rerun_seen.items() if c > 0 and _final_outcome.get(n) == "passed")
    rerun_failed = sorted(n for n, c in _rerun_seen.items() if c > 0 and _final_outcome.get(n) == "failed")
    quarantined = sorted(set(_quarantine_selected))
    payload = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "invocation": " ".join(sys.argv[1:]),
        "reruns_total": sum(_rerun_seen.values()),
        "rerun_passed": [{"nodeid": n, "reruns": _rerun_seen[n]} for n in rerun_passed],
        "rerun_still_failed": [{"nodeid": n, "reruns": _rerun_seen[n]} for n in rerun_failed],
        "quarantined": quarantined,
        "quarantined_count": len(quarantined),
        "collected_count": session.testscollected,
    }
    _FLAKY_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    _FLAKY_SIDECAR.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pytest_sessionfinish(session, exitstatus):
    # xdist：每个 worker 都会跑到这里，但账本只能由 master 写一份。
    # worker 各写各的会互相覆盖，最后留下哪一份取决于进程退出顺序——
    # 那就是一份「有时对有时错」的账本，比没有账本更坏。
    if hasattr(session.config, "workerinput"):
        return
    try:
        _write_flaky_sidecar(session)
    except Exception:  # noqa: BLE001 - 账本写失败绝不能把测试本身弄红
        pass


def pytest_terminal_summary(terminalreporter):
    """把被隔离的用例逐条打出来。"""
    nodeids = sorted(set(_quarantine_selected))
    if not nodeids:
        return
    writer = terminalreporter
    writer.write_sep("=", f"quarantine：{len(nodeids)} 条用例已退出主门禁")
    for nodeid in nodeids:
        writer.write_line(f"  DESELECTED {nodeid}")
    writer.write_line("")
    writer.write_line("这些用例既没有被删除，也没有被判为通过——它们的红不再计入本次门禁。")
    writer.write_line("单独运行：pytest -m quarantine")
    writer.write_line(f"账本（供周报统计）：{_FLAKY_SIDECAR}")
