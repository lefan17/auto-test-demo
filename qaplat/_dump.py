"""pytest 侧的采集插件（由 ``qaplat gate`` / ``registry build`` 以 ``-p qaplat._dump`` 加载）。

一次子进程同时拿到三样东西，避免为了凑数据把同一套用例跑三遍：

1. **用例级结论**（``cases``）——``--collect-only`` 时不产生；
2. **每个 nodeid 的 markers**（``markers``）；
3. **每个 nodeid 的源文件**（``files``）——registry 靠它读源码里的接口路径字符串。

产物是一个 JSON 文件，路径由 ``--qaplat-dump`` 指定。

xdist 限制：``-n`` 时用例级结论仍由 controller 汇总（``pytest_runtest_logreport`` 会在
controller 触发），但 markers/files 只在 worker 里可枚举，controller 侧为空。平台的四层
门禁刻意不用 ``-n``（层内耗时本来就短，且用例级结果要完整），因此这条限制不影响门禁；
真要用 ``-n`` 跑 registry 需自行补 marker 来源。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_STATE: dict[str, Any] = {
    "dump": None,
    "records": {},
    "markers": {},
    "files": {},
    "covers": {},
}


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("qaplat")
    group.addoption(
        "--qaplat-dump",
        action="store",
        default=None,
        metavar="PATH",
        help="把本轮采集到的用例级结果 / markers / 源文件写成 JSON（供 qaplat 读取）",
    )


def pytest_configure(config: Any) -> None:
    _STATE["dump"] = config.getoption("--qaplat-dump", None)
    _STATE["records"] = {}
    _STATE["markers"] = {}
    _STATE["files"] = {}
    _STATE["covers"] = {}


def pytest_collection_modifyitems(session: Any, config: Any, items: list[Any]) -> None:
    for item in items:
        _STATE["markers"][item.nodeid] = sorted({marker.name for marker in item.iter_markers()})
        path = getattr(item, "path", None)
        if path is not None:
            _STATE["files"][item.nodeid] = str(path)
        # ``covers`` 的取值只能在收集期拿：marker 参数（``marker.args``）不进入任何
        # pytest 报告，事后无从恢复。用 marker 参数而不是正则扫源码，是为了让
        # ``covers = ("a", "b")`` 这类归并写法也能被正确读出。
        covers = [str(arg) for marker in item.iter_markers(name="covers") for arg in marker.args if arg]
        if covers:
            _STATE["covers"][item.nodeid] = sorted(set(covers))


def pytest_runtest_logreport(report: Any) -> None:
    """把 setup/call/teardown 三个阶段压成一条用例结论。

    优先级：setup 失败 → ``error``（没跑成，不是被测问题）；call 失败 → ``failed``；
    teardown 失败且前面没失败过 → ``error``；``wasxfail`` 的 skipped → ``xfailed``。

    失败时顺带留下 ``message``（``longreprtext`` 的前 2000 字符）。这是归因分类
    （``qaplat.classify``）唯一的证据来源——pytest 的报告文本不落盘就再也拿不回来，
    而事后重跑一轮只为拿到那句报错，代价是这个平台最不该浪费的东西：时间。
    """
    entry = _STATE["records"].setdefault(
        report.nodeid,
        {"nodeid": report.nodeid, "outcome": "passed", "duration_seconds": 0.0, "message": ""},
    )
    entry["duration_seconds"] += float(report.duration or 0.0)
    if report.when == "setup":
        if report.failed:
            entry["outcome"] = "error"
        elif report.skipped:
            entry["outcome"] = "skipped"
    elif report.when == "call":
        if report.failed:
            entry["outcome"] = "failed"
        elif report.skipped:
            entry["outcome"] = "xfailed" if getattr(report, "wasxfail", None) is not None else "skipped"
    elif report.when == "teardown" and report.failed and entry["outcome"] == "passed":
        entry["outcome"] = "error"
    if report.failed and not entry["message"]:
        entry["message"] = str(getattr(report, "longreprtext", "") or "")[:2000]


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    target = _STATE.get("dump")
    if not target:
        return
    config = session.config
    if hasattr(config, "workerinput"):
        # worker 不写文件，避免和 controller 抢同一个路径；结论由 controller 汇总。
        return
    payload = {
        "exitstatus": int(exitstatus),
        "cases": [
            {
                "nodeid": n,
                "outcome": r["outcome"],
                "duration_seconds": r["duration_seconds"],
                "message": r.get("message", ""),
            }
            for n, r in sorted(_STATE["records"].items())
        ],
        "markers": _STATE["markers"],
        "files": _STATE["files"],
        "covers": _STATE["covers"],
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
