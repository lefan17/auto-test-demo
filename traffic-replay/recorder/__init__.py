"""录制端：可插拔流量源 → 采样 → 脱敏 → 入库。

回放端与比对端只依赖 `replay.model` 与本文件导出的公开 API，
对「流量到底从本地 fixtures 来还是从 mitmproxy 旁路来」零感知。
"""

from __future__ import annotations

from .capture import Sampler, SamplingPolicy, sample_exchanges
from .scrubber import ScrubRule, Scrubber, default_scrubber
from .source import LocalSynthetic, ProxyTap, TrafficSource
from .store import BaselineInfo, Repository

__all__ = [
    "BaselineInfo",
    "LocalSynthetic",
    "ProxyTap",
    "Repository",
    "Sampler",
    "SamplingPolicy",
    "ScrubRule",
    "Scrubber",
    "TrafficSource",
    "default_scrubber",
    "sample_exchanges",
]
