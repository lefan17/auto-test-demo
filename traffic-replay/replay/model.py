"""冻结的跨模块数据模型与比对口径。

本文件由调度者冻结：recorder / replayer / analyzer / report 四方共享，
任何一方都不得修改这里的签名与字段语义。需要新增字段时按 §"演进" 走追加。

演进：只允许追加带默认值的字段（frozen dataclass 的默认值必须放末尾），
禁止改名字、改类型、改顺序 —— 否则录制端与回放端的落库数据不再兼容。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

# 所有 headers 的键统一小写。录制端负责归一化，
# 否则 `Content-Type` 与 `content-type` 会在比对时变成两条假差异。
Headers = dict[str, str]


def normalize_headers(raw: dict[str, str]) -> Headers:
    """把任意大小写的 header 名归一化成小写键。"""
    return {k.lower(): v for k, v in raw.items()}


# --------------------------------------------------------------------------
# 录制端产出
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedRequest:
    method: str  # 已大写
    url: str  # 绝对 URL
    path: str  # url 的 path 部分，按接口聚合时用它
    headers: Headers = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    body: bytes | None = None
    body_json: JsonValue = None


@dataclass(frozen=True)
class RecordedResponse:
    status: int
    headers: Headers = field(default_factory=dict)
    body: bytes | None = None
    body_json: JsonValue = None
    elapsed_ms: float | None = None


@dataclass(frozen=True)
class Dependency:
    """录制时识别到的数据依赖（用户/订单/商品…）。

    `ref` 是原值，`scrubbed` 是脱敏后的稳定映射值。
    回放前按 `ref` 造数，让脱敏后的请求仍能命中真实业务数据。
    """

    kind: str
    ref: str
    scrubbed: str


@dataclass(frozen=True)
class RecordedExchange:
    exchange_id: str
    recorded_at: datetime  # 必须 tz-aware（UTC）
    request: RecordedRequest
    response: RecordedResponse
    tags: dict[str, str] = field(default_factory=dict)
    deps: tuple[Dependency, ...] = ()


def make_exchange_id(request: RecordedRequest) -> str:
    """由请求派生稳定 ID。

    同一逻辑请求在两次录制之间必须得到同一个 ID，否则基线配对会全错。
    刻意不含时间戳、不含 header（token 每次都不同）、不含 query 的键序。
    """
    parts = [
        request.method.upper(),
        request.path,
        json.dumps(
            {k: sorted(v) for k, v in sorted(request.query.items())},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    ]
    if request.body is not None:
        parts.append(hashlib.sha256(request.body).hexdigest())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# 忽略规则：什么算「不一样」，必须可讨论（见选题方案 §3.4 第 6 条）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IgnoreRules:
    ignored_paths: tuple[str, ...] = ()  # 命中的 JSON 路径整段不比，如 ("$.data.created_at",)
    ignored_path_prefixes: tuple[str, ...] = ()  # 前缀匹配，如 ("$.data.trace",)
    unordered_paths: tuple[str, ...] = ()  # 这些数组路径按集合比，与顺序无关
    float_tolerance: float = 0.0  # 绝对值容差
    ignored_headers: tuple[str, ...] = ("date", "content-length", "server", "set-cookie")
    volatile_value_fingerprints: tuple[str, ...] = ()  # 命中即降级为 NOISE 的取值指纹名


# --------------------------------------------------------------------------
# 回放端产出
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayOutcome:
    exchange_id: str
    status: int | None  # 传输失败时为 None
    headers: Headers = field(default_factory=dict)
    body: bytes | None = None
    body_json: JsonValue = None
    elapsed_ms: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------
# 比对端产出
# --------------------------------------------------------------------------


class DiffLevel(StrEnum):
    """差异分级：结构 > 类型 > 值。NOISE 只由 analyzer 赋予，differ 不得产出。"""

    STRUCTURE = "structure"
    TYPE = "type"
    VALUE = "value"
    NOISE = "noise"


SEVERITY_ORDER: tuple[DiffLevel, ...] = (
    DiffLevel.STRUCTURE,
    DiffLevel.TYPE,
    DiffLevel.VALUE,
    DiffLevel.NOISE,
)


@dataclass(frozen=True)
class FieldDiff:
    path: str  # JSONPath 风格，如 "$.data.items[0].qty"
    level: DiffLevel
    expected: object = None
    actual: object = None
    reason: str | None = None  # 仅 NOISE 填，说明命中哪条忽略规则


@dataclass(frozen=True)
class ExchangeVerdict:
    exchange_id: str
    replayed_at: datetime
    elapsed_ms: float = 0.0
    matched_baseline: bool = True  # False = 基线里没有这条，属新增流量
    status_changed: bool = False
    diffs: tuple[FieldDiff, ...] = ()
    error: str | None = None  # 传输失败/超时/造数失败

    @property
    def has_real_regression(self) -> bool:
        return self.error is None and any(d.level is not DiffLevel.NOISE for d in self.diffs)


@dataclass(frozen=True)
class ReplayReport:
    baseline_id: str
    started_at: datetime
    finished_at: datetime
    verdicts: tuple[ExchangeVerdict, ...] = ()

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def failed(self) -> tuple[ExchangeVerdict, ...]:
        return tuple(v for v in self.verdicts if v.error is not None)

    @property
    def regressed(self) -> tuple[ExchangeVerdict, ...]:
        return tuple(v for v in self.verdicts if v.has_real_regression)

    @property
    def healthy(self) -> tuple[ExchangeVerdict, ...]:
        return tuple(
            v for v in self.verdicts if v.error is None and not v.has_real_regression
        )
