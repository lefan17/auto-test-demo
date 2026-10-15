"""风险排序：把「差异条数」压成「有差异的接口数」。

这是整个项目降低人工评审成本的关键口径。800 条原始差异里绝大多数是同一个
接口的同一批字段在重复抖动；如果按差异条数排队，人要点开 800 次；按**接口**
聚合，通常只剩个位数条目要人看。

排序分数 = 该接口的回放调用量 × 该接口最严重差异级别的权重。

- 严重度权重把「schema 变化最重、值变化最轻」量化：
  `STRUCTURE=1000 > TYPE=100 > VALUE=10 > NOISE=1`。
- 乘调用量是因为「高频接口出问题，影响面更大」——同一个字段差异，出现在
  每天百万次调用的列表接口上，比出现在内部健康检查接口上重要得多。
- 取**最严重**级别而不是平均，是因为一个接口只要有一次结构退化，
  就足以说明契约在动，平均会把这条信号稀释掉。

**注意 `endpoints_with_diff` 的口径**：统计的是「有非噪声差异的接口数」，
纯噪声接口单列在 `noise_only_endpoints`，不混进人工评审队列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from replay.model import (
    DiffLevel,
    ExchangeVerdict,
    FieldDiff,
    RecordedExchange,
)
from replay.differ import canonical_path, terminal_key

#: 严重度权重：结构 > 类型 > 值 > 噪声
LEVEL_WEIGHT: dict[DiffLevel, float] = {
    DiffLevel.STRUCTURE: 1000.0,
    DiffLevel.TYPE: 100.0,
    DiffLevel.VALUE: 10.0,
    DiffLevel.NOISE: 1.0,
}

_REVIEWABLE = (DiffLevel.STRUCTURE, DiffLevel.TYPE, DiffLevel.VALUE)

#: 路径里看着像 ID 的段：纯数字、UUID、长 hex —— 归一成 {id} 才能把
#: `/api/orders/1001` 与 `/api/orders/1002` 聚成同一个接口。
_DIGITS = re.compile(r"^\d+$")
_UUID_SEG = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LONG_HEX = re.compile(r"^[0-9a-f]{12,}$")


def normalize_path(path: str) -> str:
    """把带具体 ID 的路径归一成模板：`/api/orders/1001` -> `/api/orders/{id}`。"""
    if not path:
        return "/"
    parts = []
    for segment in path.split("/"):
        low = segment.lower()
        if _DIGITS.match(segment) or _UUID_SEG.match(low) or _LONG_HEX.match(low):
            parts.append("{id}")
        else:
            parts.append(segment)
    return "/".join(parts)


def endpoint_of(method: str, path: str) -> str:
    return f"{method.upper()} {normalize_path(path)}"


# --------------------------------------------------------------------------
# 明细
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskItem:
    """一条待人工评审的接口级条目。"""

    endpoint: str
    method: str
    path: str  # 归一化模板
    call_count: int  # 该接口本轮回放次数
    diff_exchange_count: int  # 其中有差异的条数
    raw_diff_count: int  # 明细差异条数（降噪后仍存在的）
    worst_level: DiffLevel
    field_paths: tuple[str, ...]  # 归一化字段路径（唯一）
    noise_only: bool = False

    @property
    def score(self) -> float:
        if self.noise_only:
            # 纯噪声接口不进评审队列，分数只用于内部排序。
            return float(self.call_count) * LEVEL_WEIGHT[DiffLevel.NOISE]
        return float(self.call_count) * LEVEL_WEIGHT[self.worst_level]


@dataclass(frozen=True)
class RiskSummary:
    """接口级聚合结果。`endpoints_with_diff` 才是给人看的口径。"""

    items: tuple[RiskItem, ...] = ()
    noise_only_items: tuple[RiskItem, ...] = ()
    total_raw_diffs: int = 0  # 降噪后仍存在的差异条数（评审口径）
    total_diff_exchanges: int = 0  # 有差异的 exchange 条数
    endpoints_total: int = 0  # 回放到的接口总数
    noise_diffs: int = 0  # 其中被降级为噪声的条数
    diffs_before_noise: int = 0  # 降噪**之前**的原始差异条数

    @property
    def endpoints_with_diff(self) -> int:
        """**核心口径**：需要人工评审的接口数（有非噪声差异）。"""
        return len(self.items)

    @property
    def endpoints_noise_only(self) -> int:
        return len(self.noise_only_items)

    @property
    def convergence_ratio(self) -> float:
        """原始差异条数 ÷ 有差异接口数。「800 条压成几个」就是它。"""
        if not self.items:
            return 0.0
        return self.diffs_before_noise / self.endpoints_with_diff

    @property
    def top(self) -> RiskItem | None:
        return self.items[0] if self.items else None


class _Acc:
    __slots__ = ("method", "path", "calls", "diff_exchanges", "raw", "levels", "fields", "noise")

    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.path = path
        self.calls = 0
        self.diff_exchanges = 0
        self.raw = 0
        self.noise = 0
        self.levels: set[DiffLevel] = set()
        self.fields: dict[str, None] = {}

    def to_item(self) -> RiskItem:
        reviewable = self.levels & set(_REVIEWABLE)
        worst = min(
            (lvl for lvl in reviewable),
            key=lambda lvl: _REVIEWABLE.index(lvl),
            default=DiffLevel.NOISE,
        )
        return RiskItem(
            endpoint=endpoint_of(self.method, self.path),
            method=self.method,
            path=self.path,
            call_count=self.calls,
            diff_exchange_count=self.diff_exchanges,
            raw_diff_count=self.raw,
            worst_level=worst,
            field_paths=tuple(sorted(self.fields)),
            noise_only=not reviewable,
        )


def build_risk(
    verdicts: Iterable[ExchangeVerdict],
    exchanges: Iterable[RecordedExchange] | Mapping[str, RecordedExchange] = (),
    *,
    include_noise_only: bool = True,
) -> RiskSummary:
    """按接口聚合差异。

    `exchanges` 用来把 `exchange_id` 映射回 `method`/`path` ——
    verdict 本身不含路由信息（录制端才有）。传 Mapping 或序列均可。
    """
    if isinstance(exchanges, Mapping):
        by_id: Mapping[str, RecordedExchange] = exchanges
    else:
        by_id = {ex.exchange_id: ex for ex in exchanges}

    accs: dict[str, _Acc] = {}
    total_raw = 0
    total_diff_exchanges = 0
    total_noise = 0

    for verdict in verdicts:
        request = by_id.get(verdict.exchange_id)
        if request is None:
            # 没有路由信息就没法按接口聚合：退化成 "UNKNOWN <id>"，
            # 宁可多一条也不要静默丢弃。
            method, path = "UNKNOWN", f"/unknown/{verdict.exchange_id}"
        else:
            method, path = request.request.method, normalize_path(request.request.path)

        key = endpoint_of(method, path)
        acc = accs.get(key) or _Acc(method, path)
        accs[key] = acc
        acc.calls += 1

        if not verdict.diffs:
            continue
        total_diff_exchanges += 1
        acc.diff_exchanges += 1
        for diff in verdict.diffs:
            if diff.level is DiffLevel.NOISE:
                total_noise += 1
                acc.noise += 1
            else:
                total_raw += 1
                acc.raw += 1
            acc.levels.add(diff.level)
            acc.fields.setdefault(canonical_path(diff.path), None)

    items: list[RiskItem] = []
    noise_only: list[RiskItem] = []
    for acc in accs.values():
        if acc.raw == 0 and acc.noise == 0:
            # 零差异接口：既不用评审，也不是噪声，只是「干净」。
            continue
        item = acc.to_item()
        (noise_only if item.noise_only else items).append(item)

    items.sort(key=lambda i: (-i.score, i.endpoint))
    noise_only.sort(key=lambda i: (-i.score, i.endpoint))

    return RiskSummary(
        items=tuple(items),
        noise_only_items=tuple(noise_only) if include_noise_only else (),
        total_raw_diffs=total_raw,
        total_diff_exchanges=total_diff_exchanges,
        endpoints_total=len(accs),
        noise_diffs=total_noise,
        # 降噪把 level 从 VALUE 改成 NOISE，差异本身仍留在 verdict 里，
        # 所以 raw + noise 就是「降噪之前」的条数。
        diffs_before_noise=total_raw + total_noise,
    )


def render_risk_table(summary: RiskSummary, limit: int | None = None) -> str:
    """把风险清单渲染成 Markdown 表格，直接贴进报告。"""
    header = (
        "| 优先级 | 接口 | 调用量 | 有差异条数 | 差异条数 | 最严重级别 | 字段 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |"
    )
    rows = []
    for i, item in enumerate(summary.items[: limit or len(summary.items)], start=1):
        fields = ", ".join(item.field_paths[:5])
        if len(item.field_paths) > 5:
            fields += f" …(+{len(item.field_paths) - 5})"
        rows.append(
            f"| {i} | `{item.endpoint}` | {item.call_count} | "
            f"{item.diff_exchange_count} | {item.raw_diff_count} | "
            f"{item.worst_level.value} | {fields} |"
        )
    return "\n".join([header, *rows])


__all__ = [
    "LEVEL_WEIGHT",
    "RiskItem",
    "RiskSummary",
    "build_risk",
    "endpoint_of",
    "normalize_path",
    "render_risk_table",
    "terminal_key",
]
