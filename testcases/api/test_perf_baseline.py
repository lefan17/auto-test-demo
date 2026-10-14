"""性能基线门禁：把 P95 从"单次请求 + 手编阈值"升级为"多次采样 + 落盘基线 + 比阈值"。

与 `test_response_time.py` 的分工必须说清楚，否则改坏门禁时无从判断谁该红：
- 本文件测的是**本地 mock_server**，可复现，是真正的性能基线；
- `test_response_time.py` 那两条打的是公网 jsonplaceholder，结果是"本机到海外的链路质量"，
  属于抖动类，**与基线无关**，只保留为连通性冒烟。

为什么基线超标要 `pytest.fail` 而不是 `assert`：
失败文案里要带上"实测/基线/阈值/样本数/最慢样本"这一整套数字，
排查的人才能一眼看出是回归还是阈值太紧；assert 的重写会把这些信息压成一行表达式的回显。
"""

from __future__ import annotations

import os
import warnings

import pytest

from testcases.api.perf_baseline import (
    DEFAULT_SAMPLES,
    ENDPOINTS,
    EndpointStat,
    PerfSettings,
    baseline_path_from_env,
    build_document,
    collect,
    endpoint_stats,
    evaluate,
    format_diff,
    load_document,
    save_document,
)

# 同时打 api 与 perf：
# - api 让 `pytest -m api` 这个"接口用例总门禁"自动包含性能基线，门禁不会因为分了组就 invisibly 消失；
# - perf 让性能采样能单独跑（`pytest -m perf`），因为并行批次里的调度抖动会污染延迟样本。
pytestmark = [pytest.mark.api, pytest.mark.perf]


def _is_update_mode() -> bool:
    """基线更新走显式环境变量。没有它就只能读，不能让一次普通回归跑就把基线改掉。"""
    return os.getenv("UPDATE_PERF_BASELINE", "0") == "1"


def test_perf_baseline_file_is_usable() -> None:
    """基线文件必须在仓库里、能解析、字段齐全。

    单独一条用例而不是让下面那条用例顺带失败：基线文件被误删或被 .gitignore 吞掉时，
    要能立刻分辨出"门禁没了基线"和"门禁发现回归"——这两件事的处置完全不同。
    """
    if _is_update_mode():
        # 重采模式下文件可能正要被首次创建，此时"读旧基线"本就该失败，判红没有意义。
        pytest.skip("UPDATE_PERF_BASELINE=1 重采模式：基线文件由采样用例重建后再校验")
    stats = endpoint_stats(load_document())
    missing = [endpoint.name for endpoint in ENDPOINTS if endpoint.name not in stats]
    assert not missing, f"基线缺少端点: {missing}"
    for name, stat in stats.items():
        for field in ("p50_ms", "p95_ms", "p99_ms", "samples"):
            assert field in stat, f"基线端点 {name} 缺少字段 {field}"
        assert stat["samples"] >= DEFAULT_SAMPLES, f"基线端点 {name} 样本数 {stat['samples']} 低于 {DEFAULT_SAMPLES}"
        assert stat["p50_ms"] <= stat["p95_ms"] <= stat["p99_ms"], f"基线端点 {name} 分位次序颠倒"


def test_p95_should_stay_within_budget(perf_session, perf_base_url) -> None:
    """核心门禁：逐个端点重采 P95，超预算即红；显著改善则提示可更新基线。

    这条用例是采集 + 判定一起做的：性能采样本身就是它的工作内容，
    拆成 fixture 只会让"到底采没采"变成隐式行为。
    """
    settings = PerfSettings.from_env()
    stats = collect(perf_session, perf_base_url, settings)

    if _is_update_mode():
        _update_baseline(stats, settings, perf_base_url)
        return

    baseline = endpoint_stats(load_document())
    failures: list[str] = []
    improvements: list[str] = []
    report: list[str] = []
    for name, measured in sorted(stats.items()):
        verdict = evaluate(measured, float(baseline[name]["p95_ms"]), settings)
        report.append(
            f"  {name:<30} P50={measured.p50_ms:7.3f} P95={measured.p95_ms:7.3f} 阈值={verdict.threshold_ms:8.3f}"
        )
        if not verdict.ok:
            failures.append(f"{name}: {verdict.message}")
        elif verdict.improved:
            improvements.append(f"{name}: {verdict.message}")

    preview = "本次采样：\n" + "\n".join(report)
    if improvements:
        # 用 warning 而不是 print：`pytest -m api` 不带 -s，print 会被捕获、等于没提示。
        # 「比以前快很多」必须可见，否则基线会一直停在一个已经过时的宽松值上，
        # 直到某天真实劣化刚好落回旧基线之下才被发现——门禁被静默放宽。
        warnings.warn(
            "性能基线可更新（未判失败）：\n" + "\n".join(f"  {line}" for line in improvements),
            stacklevel=1,
        )
    if failures:
        pytest.fail("性能基线回归：\n" + "\n".join(f"  {line}" for line in failures) + "\n" + preview)


def _update_baseline(stats: dict[str, EndpointStat], settings: PerfSettings, base_url: str) -> None:
    """显式重采入口：`UPDATE_PERF_BASELINE=1 pytest -m perf`。

    两条硬约束：
    1. 只能由一次**真实采集**写文件——数字全部来自 `stats`，代码里没有任何可手编的路径；
    2. 必须打印旧值→新值。否则"重新采集"与"悄悄放宽门禁"的输出将无法区分，
       而门禁的放松必须留痕。
    """
    path = baseline_path_from_env()
    previous = load_document(path) if path.is_file() else None
    document = build_document(stats, settings, base_url)
    assert endpoint_stats(document), "采集结果为空，拒绝写入空基线"
    save_document(document, path)

    print(f"\n[perf] 已更新基线: {path}")
    print(f"[perf] 采集时间: {document['generated_at']}  样本/端点: {settings.samples}  丢弃预热: {settings.warmup}")
    print("[perf] P95 变更（旧 → 新）:")
    for line in format_diff(previous, document):
        print(line)
    if previous is None:
        print("[perf] 注意：此前没有基线文件，以上全部为新增端点，请把该文件提交进仓库")
