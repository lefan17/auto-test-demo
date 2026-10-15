"""端到端编排：load baseline → replay → diff → classify → 汇总 ReplayReport。

各步骤都保持可替换：`replay` 步骤只依赖 `ExchangePlayer` 的三个公开签名，
`diff`/`classify` 是纯函数，`persist` 步骤对 `recorder.store` **懒加载**——
那个模块由另一个代理并行实现，导入失败时必须能降级成「只出内存报告、不落库」，
否则一个还没写好的落库端会把整条回放链路拖崩。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from replay.differ import diff_all
from replay.model import (
    ExchangeVerdict,
    IgnoreRules,
    RecordedExchange,
    ReplayOutcome,
    ReplayReport,
)
from replay.player import ExchangePlayer, ReplaySources
from analyze.classify import (
    CategorySummary,
    DiffCategory,
    NoisePolicy,
    classify_all,
)
from analyze.risk import RiskSummary, build_risk, render_risk_table


@dataclass(frozen=True)
class ReplayRun:
    """一轮回放的完整产出。"""

    report: ReplayReport
    verdicts: tuple[ExchangeVerdict, ...]
    risk: RiskSummary
    categories: CategorySummary
    outcomes: tuple[ReplayOutcome, ...] = ()
    #: 与基线的 exchange_id 配对率，用来发现「基线过期」这类问题
    matched: int = 0
    persisted: bool = False

    @property
    def unmatched(self) -> int:
        return len(self.verdicts) - self.matched

    @property
    def noise_convergence(self) -> str:
        return (
            f"{self.risk.diffs_before_noise} 条原始差异 -> "
            f"{self.risk.endpoints_with_diff} 个有差异接口"
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "baseline_id": self.report.baseline_id,
                "started_at": self.report.started_at.isoformat(),
                "finished_at": self.report.finished_at.isoformat(),
                "total": self.report.total,
                "matched": self.matched,
                "unmatched": self.unmatched,
                "failed": len(self.report.failed),
                "regressed": len(self.report.regressed),
                "healthy": len(self.report.healthy),
                "noise_diffs": self.risk.noise_diffs,
                "diffs_before_noise": self.risk.diffs_before_noise,
                "raw_diffs": self.risk.total_raw_diffs,
                "endpoints_with_diff": self.risk.endpoints_with_diff,
                "endpoints_total": self.risk.endpoints_total,
                "categories": self.categories.counts,
                "top": [
                    {
                        "endpoint": item.endpoint,
                        "score": item.score,
                        "worst_level": item.worst_level.value,
                        "fields": list(item.field_paths),
                    }
                    for item in self.risk.items[:10]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    def render_markdown(self) -> str:
        c = self.categories.counts
        lines = [
            "# 回放差异报告",
            "",
            f"- 基线：`{self.report.baseline_id}`",
            f"- 回放条数：{self.report.total}（匹配基线 {self.matched} / 新增 {self.unmatched}）",
            f"- 环境失败：{c.get(DiffCategory.ENV_FAILURE.value, 0)}",
            f"- 真实回归：{c.get(DiffCategory.REGRESSION.value, 0)}",
            f"- 契约内新增：{c.get(DiffCategory.NEW_CONTRACT.value, 0)}",
            f"- 噪声：{c.get(DiffCategory.NOISE.value, 0)}",
            f"- 收敛：{self.noise_convergence}（差异条数 {self.risk.total_diff_exchanges}）",
            "",
            "## 待人工评审",
            "",
            render_risk_table(self.risk) or "（无）",
            "",
        ]
        reasons = self.categories.noise_reason_counts
        if reasons:
            lines += ["## 噪声来源", ""]
            lines += [f"- {k}: {v} 条" for k, v in sorted(reasons.items())]
            lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 落库（可选）
# --------------------------------------------------------------------------


def load_repository(db_path: str | Path):
    """懒加载 `recorder.store.Repository`；不可用时返回 None。"""
    try:
        module = importlib.import_module("recorder.store")
    except Exception:  # ImportError 或模块自身还没写完导致的任何导入期异常
        return None
    repository_cls = getattr(module, "Repository", None)
    if repository_cls is None:
        return None
    repo = repository_cls(db_path)
    repo.migrate()
    return repo


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def run_replay(
    exchanges: Sequence[RecordedExchange],
    player: ExchangePlayer,
    *,
    rules: IgnoreRules | None = None,
    baseline_id: str = "",
    policy: NoisePolicy | None = None,
) -> ReplayRun:
    """在给定回放器上跑一轮：replay → diff → classify → 聚合。"""
    started_at = datetime.now(timezone.utc)
    outcomes = player.run(exchanges)
    finished_at = datetime.now(timezone.utc)

    raw_verdicts = diff_all(exchanges, outcomes, rules)
    verdicts, categories = classify_all(raw_verdicts, rules, policy)
    risk = build_risk(verdicts, exchanges)

    report = ReplayReport(
        baseline_id=baseline_id,
        started_at=started_at,
        finished_at=finished_at,
        verdicts=verdicts,
    )
    return ReplayRun(
        report=report,
        verdicts=verdicts,
        risk=risk,
        categories=categories,
        outcomes=outcomes,
        matched=sum(1 for v in verdicts if v.matched_baseline),
    )


def replay_baseline(
    db_path: str | Path,
    baseline_id: str,
    base_url: str,
    *,
    rules: IgnoreRules | None = None,
    policy: NoisePolicy | None = None,
    persist: bool = True,
    repository=None,
    **player_kwargs,
) -> ReplayRun:
    """从库里读基线 → 回放 → 比对 → 分类 →（可选）落库。

    `recorder.store` 不可用时：抛 `RuntimeError` 而不是静默返回空报告——
    「读不到基线」和「基线是空的」必须在调用方眼里可区分。
    """
    repo = repository if repository is not None else load_repository(db_path)
    if repo is None:
        raise RuntimeError(
            "recorder.store 不可用：无法读取基线，请确认录制端已实现 Repository"
        )
    exchanges = repo.load_baseline(baseline_id)
    run = run_replay(
        exchanges,
        ExchangePlayer(base_url, **player_kwargs),
        rules=rules,
        baseline_id=baseline_id,
        policy=policy,
    )
    if persist:
        repo.save_report(run.report)
        run = _mark_persisted(run)
    return run


def _mark_persisted(run: ReplayRun) -> ReplayRun:
    return ReplayRun(
        report=run.report,
        verdicts=run.verdicts,
        risk=run.risk,
        categories=run.categories,
        outcomes=run.outcomes,
        matched=run.matched,
        persisted=True,
    )


def make_baseline_id(exchanges: Sequence[RecordedExchange]) -> str:
    """基线 id 缺省值：用配方条数与首条 id 派生，方便报告里人眼对齐。"""
    if not exchanges:
        return "empty"
    return f"{exchanges[0].exchange_id}-{len(exchanges)}"


__all__ = [
    "ReplayRun",
    "load_repository",
    "make_baseline_id",
    "replay_baseline",
    "run_replay",
]
