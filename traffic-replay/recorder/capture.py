"""采样策略：按路径权重、按比例、按标签筛选，且完全确定性。

确定性是硬要求：同一批输入 + 同一种子必须得到同一批输出，
否则「同一基线两次回放结果不同」会被误判成回归。
因此这里用 `blake2b(seed|exchange_id)` 取均匀随机数，
而**不是** Python 的 `hash()`（后者带进程级随机化，跨进程不稳定）。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from replay.model import RecordedExchange

__all__ = ["Sampler", "SamplingPolicy", "sample_exchanges"]

_TWO64 = float(2**64)


@dataclass(frozen=True)
class SamplingPolicy:
    """采样规则。

    - `sample_rate` 基础保留比例（0 全丢，1 全留）。
    - `path_weights` 路径权重，与 `sample_rate` 相乘后截断到 [0,1]；
      键支持精确路径与 `*` 结尾的前缀模式（如 `/api/v1/admin/*`）。
      权重 0 = 该接口一律不录，2 = 该接口按 2 倍比例录。
    - `require_tags` 必须全部命中的标签；`forbid_tags` 命中任一即丢弃。
    - `max_per_path` 每个路径最多保留多少条（流式限流，用于压制高频接口）。
    """

    sample_rate: float = 1.0
    path_weights: Mapping[str, float] = field(default_factory=dict)
    default_weight: float = 1.0
    require_tags: Mapping[str, str] = field(default_factory=dict)
    forbid_tags: Mapping[str, str] = field(default_factory=dict)
    max_per_path: int | None = None
    seed: int = 0


class Sampler:
    """把策略施加到流量上；无状态判定 + 有状态的逐路径限流。"""

    def __init__(self, policy: SamplingPolicy | None = None) -> None:
        self.policy = policy or SamplingPolicy()
        if not 0.0 <= self.policy.sample_rate:
            raise ValueError("sample_rate 不得为负")

    # -- 判定 --------------------------------------------------------------

    def weight_for(self, path: str) -> float:
        weights = self.policy.path_weights
        if path in weights:
            return float(weights[path])
        prefix_hit: tuple[str, float] | None = None
        for pattern, weight in sorted(weights.items()):
            if pattern.endswith("*") and path.startswith(pattern[:-1]):
                if prefix_hit is None or len(pattern) > len(prefix_hit[0]):
                    prefix_hit = (pattern, float(weight))
        return prefix_hit[1] if prefix_hit is not None else float(self.policy.default_weight)

    def probability(self, exchange: RecordedExchange) -> float:
        raw = self.policy.sample_rate * self.weight_for(exchange.request.path)
        return 0.0 if raw <= 0.0 else (1.0 if raw >= 1.0 else raw)

    def _unit_interval(self, exchange: RecordedExchange) -> float:
        key = f"{self.policy.seed}:{exchange.exchange_id}".encode("utf-8")
        return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big") / _TWO64

    def tags_ok(self, exchange: RecordedExchange) -> bool:
        for key, want in sorted(self.policy.require_tags.items()):
            if exchange.tags.get(key) != want:
                return False
        for key, want in sorted(self.policy.forbid_tags.items()):
            if exchange.tags.get(key) == want:
                return False
        return True

    def keep(self, exchange: RecordedExchange) -> bool:
        """是否保留该交换（不含 `max_per_path` 的跨条状态）。"""
        if not self.tags_ok(exchange):
            return False
        prob = self.probability(exchange)
        if prob <= 0.0:
            return False
        if prob >= 1.0:
            return True
        return self._unit_interval(exchange) < prob

    # -- 施加 --------------------------------------------------------------

    def sample(self, exchanges: Iterable[RecordedExchange]) -> Iterator[RecordedExchange]:
        counts: dict[str, int] = defaultdict(int)
        cap = self.policy.max_per_path
        for exchange in exchanges:
            if not self.keep(exchange):
                continue
            if cap is not None:
                path = exchange.request.path
                if counts[path] >= cap:
                    continue
                counts[path] += 1
            yield exchange


def sample_exchanges(
    exchanges: Iterable[RecordedExchange],
    policy: SamplingPolicy | None = None,
) -> Iterator[RecordedExchange]:
    return Sampler(policy).sample(exchanges)
