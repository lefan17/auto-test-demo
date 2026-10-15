"""失败归因分类。

一轮红灯里往往混着四种完全不同的东西，混在一起看等于什么都看不出来：

======================  ====================================================
``environment`` 环境      浏览器没装、端口被占、依赖缺失、连不上被测服务
``data``        数据      夹具/种子数据缺失、唯一约束冲突、测试数据过期
``defect``      被测缺陷  用例本来就是在探这个已知缺陷（``xfail(reason="DEF-xx")``）
``regression``  真实回归  本该通过的用例现在失败了——这是唯一值得拉响警报的
``unknown``     未知      没有足够证据，**不猜**，如实标出
======================  ====================================================

判定顺序是「先证环境、再证数据、再认缺陷、剩下降级为回归」。这个顺序有实际含义：
环境类证据（``PermissionError``、连接拒绝）比结论性证据更强，先排掉它们才能让
「真实回归」这个类别保持高信号——一个把环境故障也算成回归的报表，三次之后没人再看。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Attribution(StrEnum):
    """失败归因。"""

    ENVIRONMENT = "environment"
    DATA = "data"
    DEFECT = "defect"
    REGRESSION = "regression"
    UNKNOWN = "unknown"


#: 报表里用的中文标签。
LABELS: dict[Attribution, str] = {
    Attribution.ENVIRONMENT: "环境/基础设施",
    Attribution.DATA: "测试数据",
    Attribution.DEFECT: "被测缺陷（已知）",
    Attribution.REGRESSION: "真实回归",
    Attribution.UNKNOWN: "未知",
}

#: 环境类证据。都是「根本没跑到被测逻辑」的信号。
_ENVIRONMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"PermissionError",
        r"playwright",
        r"Executable doesn't exist",
        r"browser_type\.launch",
        r"ConnectionRefusedError",
        r"Connection refused",
        r"Max retries exceeded",
        r"NewConnectionError",
        r"ConnectError",
        r"Address already in use",
        r"WinError 10048",
        r"No module named",
        r"ModuleNotFoundError",
        r"ImportError",
        r"fixture .* not found",
        r"TimeoutError",
        r"timed out",
        r"TemporaryDirectory",
        r"\b50[234]\b",
        r"Failed to establish",
    )
)

#: 数据类证据。
_DATA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"IntegrityError",
        r"UNIQUE constraint",
        r"NOT NULL constraint",
        r"OperationalError",
        r"no such table",
        r"KeyError",
        r"IndexError",
        r"测试数据",
        r"种子数据",
        r"\bseed\b",
        r"data/config",
    )
)

#: 已知缺陷证据：用例显式标注的缺陷编号。
_DEFECT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in (r"\bDEF-\d+", r"已知缺陷", r"xfail", r"\bWONTFIX\b")
)


@dataclass(frozen=True)
class Failure:
    """一条失败用例的归因输入。"""

    case_id: str
    outcome: str
    message: str = ""
    markers: tuple[str, ...] = ()


def _matches(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def attribute(outcome: str, message: str = "", markers: Sequence[str] = ()) -> Attribution | None:
    """给单条用例结论归因；``passed`` / ``skipped`` 不是失败，返回 ``None``。

    规则刻意简单且可枚举——归因是要被人质疑的结论，一条正则一行输出，
    比一个说不清理由的模型预测更值得信任。
    """
    if outcome not in ("failed", "error", "xfailed"):
        return None
    if outcome == "xfailed":
        return Attribution.DEFECT
    haystack = f"{message}\n{' '.join(markers)}"
    if _matches(_ENVIRONMENT_PATTERNS, haystack):
        return Attribution.ENVIRONMENT
    if _matches(_DATA_PATTERNS, haystack):
        return Attribution.DATA
    if _matches(_DEFECT_PATTERNS, haystack):
        return Attribution.DEFECT
    if outcome == "error":
        # error 但没有环境特征：夹具/进程异常，证据不足，不猜成回归。
        return Attribution.UNKNOWN
    return Attribution.REGRESSION


def attribute_failures(failures: Iterable[Failure]) -> dict[Attribution, list[Failure]]:
    """把一批失败按归因分组，组内保持输入顺序。"""
    grouped: dict[Attribution, list[Failure]] = {}
    for failure in failures:
        verdict = attribute(failure.outcome, failure.message, failure.markers)
        if verdict is None:
            continue
        grouped.setdefault(verdict, []).append(failure)
    return grouped


def summarize(failures: Iterable[Failure]) -> dict[Attribution, int]:
    """归因计数，未出现的类别补 0，方便报表稳定渲染。"""
    counts: dict[Attribution, int] = {a: 0 for a in Attribution}
    for verdict, items in attribute_failures(failures).items():
        counts[verdict] = len(items)
    return counts


def verdict_text(counts: Mapping[Attribution, int]) -> str:
    """一行结论：有真实回归就明说，没有就说清楚红灯是什么性质。"""
    regressions = counts.get(Attribution.REGRESSION, 0)
    if regressions:
        return f"存在 {regressions} 条真实回归，必须处理"
    others = sum(v for k, v in counts.items() if k is not Attribution.REGRESSION)
    if others:
        return f"{others} 条失败全部归因于环境/数据/已知缺陷，非回归信号"
    return "无失败"
