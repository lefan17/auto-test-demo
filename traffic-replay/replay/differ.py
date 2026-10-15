"""结构化树形 diff。

口径（这就是「什么算不一样」的答案）：

- 路径统一 JSONPath 风格：`$.data.items[0].qty`；键名不是合法标识符时退化成
  `$.data["x-trace"]`，数组下标一律用 `[i]`。
- 分级只有三档，由**差异的性质**决定，与大小无关：
  - `STRUCTURE`：一边有一边没有（键增删、数组长度变化）。
  - `TYPE`：两边都有但底层类型不同（`object`/`array`/`string`/`number`/`bool`/`null`）。
    `int` 与 `float` 同属 `number`，它们的差异归 `VALUE`，不算类型变化。
  - `VALUE`：类型相同、值不同。
- **differ 不得产出 `NOISE`**。噪声是「这次差异值不值得人看」的业务判断，
  只有 analyzer 有权赋予（见 analyze/classify.py）。
- `reason` 字段只由 NOISE 填，所以这里恒为 None。
- 非 JSON 响应（纯文本/二进制/空体）只比状态码与 body 长度，不比内容 ——
  二进制响应的字节差异对人没有评审价值，而长度变化足以暴露截断/换实现。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from replay.model import (
    DiffLevel,
    ExchangeVerdict,
    FieldDiff,
    Headers,
    IgnoreRules,
    RecordedExchange,
    RecordedResponse,
    ReplayOutcome,
    normalize_headers,
)
from datetime import datetime, timezone

#: 结构差异里「这一侧压根没有这个键」的占位符（不能复用 None，None 是合法 JSON 值）。
MISSING = "<missing>"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INDEX = re.compile(r"\[\d+\]")


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def child_path(path: str, key: str) -> str:
    if _IDENT.fullmatch(key):
        return f"{path}.{key}" if path != "$" else f"$.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def index_path(path: str, index: int) -> str:
    return f"{path}[{index}]"


def canonical_path(path: str) -> str:
    """把 `$.a.b[3].c` 归一成 `$.a.b[].c`，用于按接口/字段名做跨条目聚合。"""
    return _INDEX.sub("[]", path)


def terminal_key(path: str) -> str:
    """取路径最后一段的字段名（去掉数组下标）。"""
    stripped = _INDEX.sub("", path)
    if stripped.endswith("]"):
        # 形如 $.data["x-trace"]
        return stripped[stripped.rfind("[") + 1 : -1].strip("\"'")
    return stripped.rsplit(".", 1)[-1]


# --------------------------------------------------------------------------
# 忽略规则匹配
# --------------------------------------------------------------------------


def is_ignored(path: str, rules: IgnoreRules) -> bool:
    """命中即整段不比：精确命中 `ignored_paths`，或前缀命中 `ignored_path_prefixes`。"""
    if path in rules.ignored_paths:
        return True
    return any(path.startswith(p) for p in rules.ignored_path_prefixes)


def is_unordered(path: str, rules: IgnoreRules) -> bool:
    """数组是否顺序无关。规则给 `$.data.items` 时，其下标子路径也算命中。"""
    for rule in rules.unordered_paths:
        if path == rule or path.startswith(rule + "[") or path.startswith(rule + "."):
            return True
    return False


# --------------------------------------------------------------------------
# 类型与值
# --------------------------------------------------------------------------


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)


# --------------------------------------------------------------------------
# 核心递归
# --------------------------------------------------------------------------


def diff_json(
    expected: Any,
    actual: Any,
    rules: IgnoreRules,
    path: str = "$",
    out: list[FieldDiff] | None = None,
) -> tuple[FieldDiff, ...]:
    """递归比较两棵 JSON 树，返回差异元组（顺序稳定：按路径深度优先）。"""
    sink: list[FieldDiff] = [] if out is None else out
    _walk(expected, actual, rules, path, sink)
    return tuple(sink) if out is None else ()


def _walk(
    expected: Any,
    actual: Any,
    rules: IgnoreRules,
    path: str,
    out: list[FieldDiff],
) -> None:
    if is_ignored(path, rules):
        return

    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected:
            if key not in actual:
                out.append(
                    FieldDiff(
                        path=child_path(path, key),
                        level=DiffLevel.STRUCTURE,
                        expected=expected[key],
                        actual=MISSING,
                    )
                )
        for key in actual:
            if key not in expected:
                out.append(
                    FieldDiff(
                        path=child_path(path, key),
                        level=DiffLevel.STRUCTURE,
                        expected=MISSING,
                        actual=actual[key],
                    )
                )
        for key in expected:
            if key in actual:
                _walk(expected[key], actual[key], rules, child_path(path, key), out)
        return

    if isinstance(expected, list) and isinstance(actual, list):
        if is_unordered(path, rules):
            _walk_unordered(expected, actual, rules, path, out)
        else:
            _walk_ordered(expected, actual, rules, path, out)
        return

    if type_name(expected) != type_name(actual):
        out.append(
            FieldDiff(
                path=path,
                level=DiffLevel.TYPE,
                expected=expected,
                actual=actual,
            )
        )
        return

    if _is_number(expected) and _is_number(actual):
        tolerance = rules.float_tolerance
        if tolerance and abs(float(expected) - float(actual)) <= tolerance:
            return
        if expected != actual:
            out.append(
                FieldDiff(
                    path=path,
                    level=DiffLevel.VALUE,
                    expected=expected,
                    actual=actual,
                )
            )
        return

    if expected != actual:
        out.append(
            FieldDiff(
                path=path,
                level=DiffLevel.VALUE,
                expected=expected,
                actual=actual,
            )
        )


def _walk_ordered(
    expected: list, actual: list, rules: IgnoreRules, path: str, out: list[FieldDiff]
) -> None:
    for i, item in enumerate(expected):
        if i >= len(actual):
            out.append(
                FieldDiff(
                    path=index_path(path, i),
                    level=DiffLevel.STRUCTURE,
                    expected=item,
                    actual=MISSING,
                )
            )
        else:
            _walk(item, actual[i], rules, index_path(path, i), out)
    for i in range(len(expected), len(actual)):
        out.append(
            FieldDiff(
                path=index_path(path, i),
                level=DiffLevel.STRUCTURE,
                expected=MISSING,
                actual=actual[i],
            )
        )


def _walk_unordered(
    expected: list, actual: list, rules: IgnoreRules, path: str, out: list[FieldDiff]
) -> None:
    """顺序无关数组按集合比：先各自排序成规范序，再逐位比。"""
    left = sorted(expected, key=_sort_key)
    right = sorted(actual, key=_sort_key)
    if [_sort_key(x) for x in left] == [_sort_key(x) for x in right]:
        return
    _walk_ordered(left, right, rules, path, out)


# --------------------------------------------------------------------------
# 响应比对
# --------------------------------------------------------------------------


def diff_headers(
    expected: Headers, actual: Headers, rules: IgnoreRules
) -> tuple[FieldDiff, ...]:
    """比 header：先按小写归一，忽略 `ignored_headers`，只报「新增/缺失/值变」。"""
    ignored = {h.lower() for h in rules.ignored_headers}
    left = normalize_headers(expected)
    right = normalize_headers(actual)
    out: list[FieldDiff] = []
    for key in sorted(set(left) | set(right)):
        if key in ignored:
            continue
        path = child_path("$.headers", key)
        if key not in right:
            out.append(FieldDiff(path, DiffLevel.VALUE, left[key], MISSING))
        elif key not in left:
            out.append(FieldDiff(path, DiffLevel.VALUE, MISSING, right[key]))
        elif left[key] != right[key]:
            out.append(FieldDiff(path, DiffLevel.VALUE, left[key], right[key]))
    return tuple(out)


def _has_json(response: RecordedResponse | ReplayOutcome) -> bool:
    return response.body_json is not None


def _body_len(response: RecordedResponse | ReplayOutcome) -> int:
    return len(response.body) if response.body is not None else 0


def diff_exchange(
    baseline: RecordedExchange,
    outcome: ReplayOutcome,
    rules: IgnoreRules | None = None,
) -> ExchangeVerdict:
    """把一条回放结果与基线配对比较，产出 ExchangeVerdict。"""
    rules = rules or IgnoreRules()
    expected = baseline.response

    if outcome.error is not None:
        # 传输失败：没有可比对象，交给 analyzer 归为「环境失败」。
        return ExchangeVerdict(
            exchange_id=outcome.exchange_id,
            replayed_at=datetime.now(timezone.utc),
            elapsed_ms=outcome.elapsed_ms,
            matched_baseline=True,
            diffs=(),
            error=outcome.error,
        )

    diffs: list[FieldDiff] = []
    status_changed = expected.status != outcome.status
    if status_changed:
        diffs.append(
            FieldDiff("$.status", DiffLevel.VALUE, expected.status, outcome.status)
        )

    if _has_json(expected) and _has_json(outcome):
        diff_json(expected.body_json, outcome.body_json, rules, "$", diffs)
    elif _has_json(expected) != _has_json(outcome):
        diffs.append(
            FieldDiff(
                "$.body",
                DiffLevel.TYPE,
                _describe_body(expected),
                _describe_body(outcome),
            )
        )
    else:
        # 非 JSON：只比长度，不比内容。
        if _body_len(expected) != _body_len(outcome):
            diffs.append(
                FieldDiff("$.body", DiffLevel.VALUE, _body_len(expected), _body_len(outcome))
            )

    diffs.extend(diff_headers(expected.headers, outcome.headers, rules))

    return ExchangeVerdict(
        exchange_id=outcome.exchange_id,
        replayed_at=datetime.now(timezone.utc),
        elapsed_ms=outcome.elapsed_ms,
        matched_baseline=True,
        status_changed=status_changed,
        diffs=tuple(diffs),
        error=None,
    )


def new_traffic_verdict(outcome: ReplayOutcome) -> ExchangeVerdict:
    """基线里没有这条流量：matched_baseline=False，不做比对。"""
    return ExchangeVerdict(
        exchange_id=outcome.exchange_id,
        replayed_at=datetime.now(timezone.utc),
        elapsed_ms=outcome.elapsed_ms,
        matched_baseline=False,
        status_changed=False,
        diffs=(),
        error=outcome.error,
    )


def _describe_body(response: RecordedResponse | ReplayOutcome) -> str:
    if _has_json(response):
        return "json"
    content_type = normalize_headers(response.headers).get("content-type", "")
    if content_type:
        return f"bytes({content_type})"
    return "bytes"


def diff_all(
    baselines: Iterable[RecordedExchange],
    outcomes: Iterable[ReplayOutcome],
    rules: IgnoreRules | None = None,
) -> tuple[ExchangeVerdict, ...]:
    """按 exchange_id 配对；基线缺失的 outcome 记为新增流量，顺序跟随 outcomes。"""
    index: Mapping[str, RecordedExchange] = {
        ex.exchange_id: ex for ex in baselines
    }
    results = []
    for outcome in outcomes:
        baseline = index.get(outcome.exchange_id)
        if baseline is None:
            results.append(new_traffic_verdict(outcome))
        else:
            results.append(diff_exchange(baseline, outcome, rules))
    return tuple(results)
