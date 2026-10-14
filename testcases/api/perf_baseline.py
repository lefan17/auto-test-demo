"""性能基线的纯逻辑层：分位统计、基线读写、阈值判定、diff 渲染。

为什么要独立成一个模块而不是塞进用例文件：
基线有三个消费者——门禁用例、一键重采脚本、门禁自身的元测试——若判定逻辑只存在于
用例里，重采脚本就得"再实现一遍阈值公式"，两份实现迟早飘掉，而门禁飘掉是不报错的。
所以这里只放可被同时复用的纯函数，不 import pytest、不起服务。

为什么用 nearest-rank 而不是线性插值分位：
样本量 N=100 时线性插值会给出一个**从未真实发生过**的延迟值（例如第 94.5 个样本），
门禁报告的数字就没法对着任何一次真实请求复核。nearest-rank 给出的永远是观测到的样本，
"这条 P95 对应哪次请求"可以当场指出来——排查性能回归时这比插值的平滑性重要得多。

为什么阈值是 `基线 P95 × 倍数 + 绝对下限`：
倍数吸收"同一份代码在不同机器上的系统性差异"，绝对下限吸收"基线本身只有几毫秒时，
倍数放大不出有意义的分辨率"。本地 mock_server 的 P95 实测 3~6ms，
纯倍数会让 1ms 的调度抖动直接变成红灯——那不是回归，是噪声。
"""

from __future__ import annotations

import json
import math
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "testcases" / "api" / "perf_baseline.json"

SCHEMA_VERSION = 1

# 采样默认值。N=100 的依据：nearest-rank 的 P99 需要 N≥100 才有第 99 个顺序统计量，
# 否则 P99 只能退化成最大值，报出来的数没有分辨力；同一梯度 P95 容忍 5 个最慢样本不入账，
# 单端点采 100 次在本地约 0.5s，成本可以忽略。
DEFAULT_SAMPLES = 100
# 前若干次请求要付连接建立、SQLite 页缓存冷启、Pydantic 模型首次编译的固定开销，
# 它们不是"稳态延迟"，计入分位只会把基线抬高一截。丢弃前 10 次。
DEFAULT_WARMUP = 10

# 倍数 3.0 与下限 10ms 的依据（原始数据见 docs/wave3-perf-baseline-handoff.md §5.4）：
# 同一梯度连续三次采集的轮间波动最大 1.25x；把任务期间八次同口径采集一并计入则最大 1.96x。
# 更关键的是最坏组合：拿**最低**那次的基线算阈值、拿**最高**那次的实测去比，
# 六条端点仍全部有 4 倍以上余量（最紧的 /inventory/stock 是 23.25ms vs 实测 5.399ms）。
# 10ms 下限是这里的主要安全垫：最轻的端点（P95 约 2.8ms）必须劣化 6.6 倍以上才会红，
# 所以 CI 上 4 个并行 worker 的调度抖动打不红它（已在 -n 4 下实测全绿）。
#
# 这个组合的定位要说清楚：它拦的是**量级劣化**（漏索引、N+1 查询、同步阻塞变大），
# 不是几毫秒的漂移。想要更严只需调 PERF_P95_FACTOR / PERF_P95_FLOOR_MS，不必改代码。
DEFAULT_FACTOR = 3.0
DEFAULT_FLOOR_MS = 10.0
# 实测基线低于 `基线 × 该比例` 时提示可以重采基线（不判失败）。
# 选 0.5：性能翻倍改善才值得动基线文件，否则每次采集都提示等于没提示。
DEFAULT_IMPROVE_RATIO = 0.5


@dataclass(frozen=True)
class Endpoint:
    """一个被测端点。只采只读 GET：性能基线不该改变被测数据。"""

    name: str
    method: str
    path: str


# 选点逻辑：覆盖 mock_server 的四种典型查询路径，而不是把 20 多个端点全采一遍。
# - /healthz：不含业务 SQL，是"框架 + 进程"的地板延迟，作为对照组；
# - /materials：分页列表 + 排序白名单解析 + 一次 SQL；
# - /materials/{code}：路径参数 + 主键查询，与列表形成对照；
# - /inventory/stock：带别名排序的库存视图，是本服务较重的只读查询；
# - /work-orders：另一张业务表，防止基线只覆盖物料域；
# - /inventory/ledger：流水表分页，行数随业务增长，是最可能先劣化的那条。
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("GET /healthz", "GET", "/healthz"),
    Endpoint("GET /materials?page_size=20", "GET", "/materials?page_size=20"),
    Endpoint("GET /materials/MAT-CU-001", "GET", "/materials/MAT-CU-001"),
    Endpoint("GET /inventory/stock", "GET", "/inventory/stock"),
    Endpoint("GET /work-orders?page_size=20", "GET", "/work-orders?page_size=20"),
    Endpoint("GET /inventory/ledger", "GET", "/inventory/ledger"),
)


@dataclass(frozen=True)
class EndpointStat:
    """一个端点的采集结果（毫秒）。"""

    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    samples: int


@dataclass(frozen=True)
class PerfSettings:
    """一次门禁/采集用的参数，全部可由环境变量覆盖。"""

    samples: int
    warmup: int
    factor: float
    floor_ms: float
    limit_ms: float | None
    improve_ratio: float

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PerfSettings:
        source = os.environ if env is None else env
        raw_limit = (source.get("PERF_P95_LIMIT_MS") or "").strip()
        return cls(
            samples=int(source.get("PERF_SAMPLES") or DEFAULT_SAMPLES),
            warmup=int(source.get("PERF_WARMUP") or DEFAULT_WARMUP),
            factor=float(source.get("PERF_P95_FACTOR") or DEFAULT_FACTOR),
            floor_ms=float(source.get("PERF_P95_FLOOR_MS") or DEFAULT_FLOOR_MS),
            limit_ms=float(raw_limit) if raw_limit else None,
            improve_ratio=float(source.get("PERF_IMPROVE_RATIO") or DEFAULT_IMPROVE_RATIO),
        )


@dataclass(frozen=True)
class Verdict:
    """一次门禁判定的结果。判定逻辑做成可单独调用的纯函数，门禁本身才有元测试。"""

    ok: bool
    measured_p95_ms: float
    baseline_p95_ms: float
    threshold_ms: float
    improved: bool
    message: str


def percentile(values: list[float], quantile: float) -> float:
    """nearest-rank 分位：返回真实观测到的样本，不做插值。

    rank = ceil(q × N)，取排序后第 rank 个（1-based）。q=0.95、N=100 时 rank=95，
    即"最多 5% 的样本比它慢"。
    """
    if not values:
        raise ValueError("分位统计需要至少一个样本")
    if not 0 < quantile <= 1:
        raise ValueError(f"quantile 必须落在 (0, 1]，实际 {quantile}")
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: list[float]) -> EndpointStat:
    return EndpointStat(
        p50_ms=percentile(samples, 0.50),
        p95_ms=percentile(samples, 0.95),
        p99_ms=percentile(samples, 0.99),
        max_ms=max(samples),
        mean_ms=sum(samples) / len(samples),
        samples=len(samples),
    )


def resolve_threshold(baseline_p95_ms: float, settings: PerfSettings) -> float:
    """把基线 P95 换算成门禁阈值。

    `PERF_P95_LIMIT_MS` 设了就取更严的一侧（min）。它的用途不是"放宽 CI"，
    而是给出一个**与基线无关的绝对上限**：本地想验证"门禁真的在判"时，
    把它设成 1 即可让全部端点必然失败，不必去手编基线数字。
    """
    threshold = baseline_p95_ms * settings.factor + settings.floor_ms
    if settings.limit_ms is not None:
        threshold = min(threshold, settings.limit_ms)
    return threshold


def evaluate(measured: EndpointStat, baseline_p95_ms: float, settings: PerfSettings) -> Verdict:
    """比对一次采集与基线：超过阈值判失败，显著变快则提示可重采基线。"""
    threshold = resolve_threshold(baseline_p95_ms, settings)
    ok = measured.p95_ms <= threshold
    improved = baseline_p95_ms > 0 and measured.p95_ms < baseline_p95_ms * settings.improve_ratio
    if not ok:
        message = (
            f"P95 {measured.p95_ms:.3f}ms 超出性能预算 {threshold:.3f}ms"
            f"（基线 {baseline_p95_ms:.3f}ms × {settings.factor} + {settings.floor_ms}ms 下限，"
            f"样本 {measured.samples} 次，最慢 {measured.max_ms:.3f}ms）"
        )
    elif improved:
        message = (
            f"P95 {measured.p95_ms:.3f}ms 明显优于基线 {baseline_p95_ms:.3f}ms"
            f"（< {settings.improve_ratio} 倍）：可执行 UPDATE_PERF_BASELINE=1 重采基线"
        )
    else:
        message = f"P95 {measured.p95_ms:.3f}ms ≤ 阈值 {threshold:.3f}ms（基线 {baseline_p95_ms:.3f}ms）"
    return Verdict(
        ok=ok,
        measured_p95_ms=measured.p95_ms,
        baseline_p95_ms=baseline_p95_ms,
        threshold_ms=threshold,
        improved=improved,
        message=message,
    )


def collect(
    session: requests.Session,
    base_url: str,
    settings: PerfSettings,
    endpoints: tuple[Endpoint, ...] = ENDPOINTS,
    timeout: float = 15.0,
) -> dict[str, EndpointStat]:
    """按端点逐个采样。每个端点先丢弃 warmup 次，再记录 samples 次。

    令牌由调用方挂在 session 头上：性能基线只想量业务查询，
    每个样本都重新登录会把密码哈希的固定开销混进分位，而那是与查询性能无关的常数。

    用 `time.perf_counter()` 手工计时而不是 `requests` 的 `response.elapsed`：
    这里量的是"从发起请求到拿到响应"的端到端墙钟，且不受 apis 层重试逻辑影响——
    性能基线要盯住被测服务本身，重试恰好会把服务端的劣化掩盖掉。

    采集期间状态码不是 200 就直接抛错：一份采自错误响应的基线毫无意义，
    而不做这个检查时它会安静地记下一个"极快"的 P95（例如 404 的短路返回）。
    """
    stats: dict[str, EndpointStat] = {}
    for endpoint in endpoints:
        url = f"{base_url}{endpoint.path}"
        for _ in range(settings.warmup):
            session.request(endpoint.method, url, timeout=timeout)
        latencies: list[float] = []
        for _ in range(settings.samples):
            started = time.perf_counter() * 1000.0
            response = session.request(endpoint.method, url, timeout=timeout)
            latencies.append(time.perf_counter() * 1000.0 - started)
            if response.status_code != 200:
                raise AssertionError(f"{endpoint.name} 采集时返回 {response.status_code}，基线必须采自健康响应")
        stats[endpoint.name] = summarize(latencies)
    return stats


def environment_fingerprint(settings: PerfSettings, base_url: str) -> dict[str, object]:
    """记录采集环境。基线脱离环境就没有可比性，这份指纹是"能不能直接比"的判据。"""
    return {
        "sut": "mock_server（本地 FastAPI + SQLite，非公网）",
        "sut_host": base_url.split("/api/")[0],
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "samples_per_endpoint": settings.samples,
        "warmup_discarded": settings.warmup,
        "percentile_method": "nearest-rank",
        "threshold_rule": f"baseline_p95 × {settings.factor} + {settings.floor_ms}ms",
    }


def build_document(stats: dict[str, EndpointStat], settings: PerfSettings, base_url: str) -> dict[str, object]:
    """组装基线文件内容。所有数字都来自 stats，不存在任何手编的余地。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "UPDATE_PERF_BASELINE=1 pytest -m perf",
        "environment": environment_fingerprint(settings, base_url),
        "endpoints": {name: asdict(stat) for name, stat in stats.items()},
    }


def load_document(path: Path | None = None) -> dict[str, object]:
    return json.loads((path or baseline_path_from_env()).read_text(encoding="utf-8"))


def save_document(data: dict[str, object], path: Path | None = None) -> Path:
    target = path or baseline_path_from_env()
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def endpoint_stats(document: dict[str, object]) -> dict[str, dict[str, Any]]:
    """取出 endpoints 段。基线文件是人工也可能手改的 JSON，所以校验非空后才返回。"""
    raw = document.get("endpoints")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("基线文件缺少非空的 endpoints 段")
    return {str(name): dict(stat) for name, stat in raw.items()}


def format_diff(old: dict[str, object] | None, new: dict[str, object]) -> list[str]:
    """渲染"旧值 → 新值"，新增/移除端点单独标注。

    更新基线必须留下可读的旧值：否则"重新采集"和"悄悄放宽门禁"在输出里长得一模一样。
    """
    new_stats = endpoint_stats(new)
    old_stats = endpoint_stats(old) if old else {}
    lines: list[str] = []
    for name in sorted(set(new_stats) | set(old_stats)):
        if name not in old_stats:
            lines.append(f"  + {name}: 新增，P95={new_stats[name]['p95_ms']:.3f}ms")
        elif name not in new_stats:
            lines.append(f"  - {name}: 移除（旧 P95={old_stats[name]['p95_ms']:.3f}ms）")
        else:
            before = float(old_stats[name]["p95_ms"])
            after = float(new_stats[name]["p95_ms"])
            lines.append(f"  ~ {name}: P95 {before:.3f}ms → {after:.3f}ms ({after - before:+.3f}ms)")
    return lines


def baseline_path_from_env(env: dict[str, str] | None = None) -> Path:
    """基线文件路径可用 `PERF_BASELINE_FILE` 重定向——元测试要靠它操作临时基线，不碰真文件。"""
    source = os.environ if env is None else env
    override = (source.get("PERF_BASELINE_FILE") or "").strip()
    return Path(override) if override else BASELINE_PATH


def main(argv: list[str] | None = None) -> int:
    """只读打印基线文件内容，便于人工核对（重采见 `--update`）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    path = baseline_path_from_env()
    if not path.is_file():
        print(f"基线文件不存在: {path}", file=sys.stderr)
        return 2
    document = load_document(path)
    print(f"基线文件: {path}")
    print(f"采集时间: {document.get('generated_at')}")
    if args and args[0] == "--json":
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0
    for name, stat in sorted(endpoint_stats(document).items()):
        print(f"  {name:<28} P50={stat['p50_ms']:.3f} P95={stat['p95_ms']:.3f} P99={stat['p99_ms']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
