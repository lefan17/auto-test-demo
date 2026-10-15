"""静态 HTML 报表：把数据库里的分散事实拼成一页可读的东西。

产出是一个目录（默认 ``qaplat/.cache/report/``），入口 ``index.html``，每页独立自包含：
不引 CDN、不引 JS 框架、不依赖网络。理由是报表最常见的读者场景是「CI 上跑完、
打包成 artifact 下载到本地打开」，任何外部资源都会让它在那个场景下变成白屏。

四个页面各自回答一个问题：

- ``runs.html``     最近几轮是什么状态？哪一层在变差？
- ``coverage.html`` 哪些 operationId 没有任何用例覆盖？（诚实性红线的可视化）
- ``attribution.html`` 红灯里有多少是真实回归、多少只是环境问题？
- ``drift.html``    契约基准有没有被悄悄改动过？
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from qaplat import classify
from qaplat.metrics import MetricsStore, RunRecord
from qaplat.paths import CACHE_DIR
from qaplat.registry import OperationCoverage, coverage_ratio, load_coverage_matrix, uncovered

#: 报表默认输出目录。
DEFAULT_REPORT_DIR = CACHE_DIR / "report"

_CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; padding: 24px 32px;
       line-height: 1.5; background: #fbfbfd; color: #1d1d1f; }
@media (prefers-color-scheme: dark) { body { background: #1c1c1e; color: #f2f2f7; } }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 8px; }
.sub { opacity: .65; font-size: 13px; margin-bottom: 20px; }
nav a { margin-right: 14px; font-size: 14px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 8px; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid rgba(128,128,128,.28); vertical-align: top; }
th { font-weight: 600; background: rgba(128,128,128,.10); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.ok { color: #1a7f37; } .bad { color: #c0392b; } .warn { color: #b8860b; } .dim { opacity: .6; }
.card { display: inline-block; padding: 10px 18px; margin: 0 10px 10px 0; border: 1px solid rgba(128,128,128,.30);
        border-radius: 8px; min-width: 120px; }
.card .k { font-size: 12px; opacity: .65; } .card .v { font-size: 20px; font-weight: 600; }
.empty { opacity: .55; font-style: italic; }
"""

_NAV = """<nav><a href="index.html">总览</a><a href="runs.html">轮次</a><a href="coverage.html">覆盖</a>\
<a href="attribution.html">失败归因</a><a href="drift.html">契约漂移</a></nav>"""


@dataclass(frozen=True)
class ReportResult:
    """``build_report`` 的返回值：输出目录与各页路径。"""

    out_dir: Path
    pages: tuple[Path, ...]
    runs_count: int
    cases_count: int
    operations_total: int
    covered_total: int
    regressions: int

    @property
    def index(self) -> Path:
        return self.out_dir / "index.html"


def escape(value: object) -> str:
    """HTML 转义。用例 nodeid 与失败报文都可能含 ``<`` / ``&``，不转义就会撕坏页面结构。"""
    return html.escape(str(value), quote=True)


def _page(title: str, body: str, generated_at: datetime | None = None) -> str:
    stamp = (generated_at or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S %Z")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>\n"
        f"<h1>{escape(title)}</h1>\n<p class='sub'>生成时间：{escape(stamp)}</p>\n{_NAV}\n{body}\n"
        "</body></html>\n"
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], *, numeric: Sequence[int] = ()) -> str:
    if not rows:
        return "<p class='empty'>（无数据）</p>"
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(
            f"<td class='num'>{cell}</td>" if index in numeric else f"<td>{cell}</td>" for index, cell in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _card(key: str, value: str, css: str = "") -> str:
    return f"<div class='card'><div class='k'>{escape(key)}</div><div class='v {css}'>{escape(value)}</div></div>"


def _rate_css(rate: float, threshold: float = 1.0) -> str:
    if rate >= threshold:
        return "ok"
    return "bad" if rate < 0.9 else "warn"


def _page_index(store: MetricsStore, coverage: Sequence[OperationCoverage], out_dir: Path) -> str:
    runs = store.recent_runs(limit=200)
    latest = store.latest_run()
    cases = _total_cases(store, runs)
    covered = sum(1 for c in coverage if c.covered_by)
    counts = _attribution_counts(store, runs)
    regressions = counts.get(classify.Attribution.REGRESSION, 0)

    cards = [
        _card("最近一轮", latest.layer if latest else "—"),
        _card(
            "最近通过率",
            f"{latest.pass_rate:.1%}" if latest else "—",
            _rate_css(latest.pass_rate, 1.0) if latest else "",
        ),
        _card("累计轮次", str(len(runs))),
        _card("用例级记录", str(cases)),
        _card(
            "接口覆盖",
            f"{covered}/{len(coverage)}" if coverage else "—",
            _rate_css(coverage_ratio(coverage)) if coverage else "",
        ),
        _card("真实回归", str(regressions), "bad" if regressions else "ok"),
    ]

    layers = _table(
        ["层", "轮次", "平均通过率", "平均耗时(s)"],
        [
            [escape(layer), str(n), f"{rate:.1%}", f"{dur:.1f}"]
            for layer, n, rate, dur in store.layer_summary(last_n_runs=200)
        ],
        numeric=(1, 2, 3),
    )

    flaky = store.flaky_cases(last_n_runs=10)
    flaky_table = _table(
        ["不稳定的用例", "通过轮次", "出现轮次"],
        [[f"<span class='mono'>{escape(case_id)}</span>", str(p), str(a)] for case_id, p, a in flaky],
        numeric=(1, 2),
    )

    drift = store.contract_drift_history(limit=1)
    drift_line = "<p class='empty'>尚未记录过契约漂移检查</p>"
    if drift:
        item = drift[0]
        mark = "<span class='bad'>已漂移</span>" if item.drifted else "<span class='ok'>一致</span>"
        drift_line = (
            f"<p>最新检查 {mark}：<span class='mono'>{escape(item.commit_sha)}</span>（{escape(item.checked_at)}）</p>"
        )

    body = (
        "".join(cards)
        + "<h2>各层概况</h2>"
        + layers
        + "<h2>跨轮次不稳定用例（最近 10 轮）</h2>"
        + flaky_table
        + "<h2>契约漂移</h2>"
        + drift_line
        + f"<p class='sub'>报表目录：<span class='mono'>{escape(out_dir)}</span></p>"
    )
    return body


def _page_runs(store: MetricsStore) -> str:
    runs = store.recent_runs(limit=50)
    rows: list[list[str]] = []
    for run in runs:
        css = _rate_css(run.pass_rate, 1.0)
        rows.append(
            [
                f"<span class='mono'>{escape(run.run_id)}</span>",
                escape(run.layer),
                f"<span class='mono'>{escape((run.commit_sha or '—')[:12])}</span>",
                escape(run.started_at.strftime("%Y-%m-%d %H:%M:%S")),
                str(run.total),
                str(run.passed),
                str(run.failed),
                str(run.errors),
                str(run.skipped),
                str(run.xfailed),
                f"<span class='{css}'>{run.pass_rate:.1%}</span>",
                f"{run.duration_seconds:.1f}",
                escape(run.outcome),
            ]
        )
    return _table(
        ["run_id", "层", "commit", "开始", "总", "通过", "失败", "错误", "跳过", "xfail", "通过率", "耗时(s)", "结论"],
        rows,
        numeric=(4, 5, 6, 7, 8, 9, 11),
    )


def _page_coverage(coverage: Sequence[OperationCoverage]) -> str:
    if not coverage:
        return (
            "<p class='empty'>没有覆盖数据。先运行 <span class='mono'>python -m qaplat.cli registry build</span>。</p>"
        )
    gaps = uncovered(coverage)
    header = (
        _card("operation 总数", str(len(coverage)))
        + _card("已覆盖", str(len(coverage) - len(gaps)))
        + _card("未覆盖", str(len(gaps)), "bad" if gaps else "ok")
        + _card("覆盖率", f"{coverage_ratio(coverage):.1%}", _rate_css(coverage_ratio(coverage)))
    )
    rows = []
    for item in coverage:
        owners = "<br>".join(f"<span class='mono'>{escape(n)}</span>" for n in item.covered_by)
        rows.append(
            [
                f"<span class='mono'>{escape(item.method)} {escape(item.path)}</span>",
                f"<span class='mono'>{escape(item.operation_id)}</span>",
                str(len(item.covered_by)),
                owners or "<span class='bad'>未覆盖</span>",
            ]
        )
    note = (
        "<p class='sub'>归属只有两个来源：<span class='mono'>@pytest.mark.covers(\"opId\")</span> "
        "或用例源码里出现的接口路径字符串。二者都没有就记为未覆盖——不做任何推断。</p>"
    )
    return (
        header
        + note
        + "<h2>逐 operation</h2>"
        + _table(["method path", "operationId", "归属用例数", "归属用例"], rows, numeric=(2,))
    )


def _page_attribution(store: MetricsStore) -> str:
    runs = store.recent_runs(limit=10)
    containers: list[str] = []
    total: dict[classify.Attribution, int] = {a: 0 for a in classify.Attribution}
    for run in runs:
        failures = _failures_of(store, run)
        counts = classify.summarize(failures)
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
        rows = [
            [
                f"<span class='mono'>{escape(f.case_id)}</span>",
                escape(f.outcome),
                escape(classify.LABELS[verdict]),
                f"<span class='mono'>{escape(f.message.replace(chr(10), ' ')[:180])}</span>"
                if f.message
                else "<span class='dim'>—</span>",
            ]
            for verdict, items in sorted(classify.attribute_failures(failures).items(), key=lambda kv: kv[0].value)
            for f in items
        ]
        summary = "、".join(f"{classify.LABELS[a]} {counts[a]}" for a in classify.Attribution if counts[a]) or "无失败"
        containers.append(
            f"<h2>{escape(run.run_id)} <span class='sub'>（{escape(run.layer)}，{escape(summary)}）</span></h2>"
            + f"<p>{escape(classify.verdict_text(counts))}</p>"
            + _table(["用例", "结论", "归因", "失败摘要"], rows)
        )

    headline = _table(
        ["归因", "条数"],
        [[escape(classify.LABELS[a]), str(total[a])] for a in classify.Attribution],
        numeric=(1,),
    )
    if not runs:
        return "<p class='empty'>（无轮次数据）</p>"
    return (
        "<h2>最近 10 轮合计</h2>"
        + headline
        + "<p class='sub'>归因规则是可枚举的文本证据匹配，见 <span class='mono'>qaplat/classify.py</span>；"
        "证据不足时归为「未知」而不是猜成回归。</p>" + "".join(containers)
    )


def _page_drift(store: MetricsStore) -> str:
    history = store.contract_drift_history(limit=50)
    if not history:
        return (
            "<p class='empty'>尚未记录过契约漂移检查。运行 "
            "<span class='mono'>python -m qaplat.cli drift record --commit &lt;sha&gt;</span>。</p>"
        )
    rows = [
        [
            f"<span class='mono'>{escape(item.commit_sha)}</span>",
            escape(item.checked_at.strftime("%Y-%m-%d %H:%M:%S")),
            "<span class='ok'>一致</span>" if not item.drifted else "<span class='bad'>已漂移</span>",
            "<br>".join(f"<span class='mono'>{escape(c)}</span>" for c in item.changed) or "<span class='dim'>—</span>",
        ]
        for item in history
    ]
    return _table(["commit", "检查时间", "结论", "变化的文件"], rows)


def _failures_of(store: MetricsStore, run: RunRecord, *, limit: int = 30) -> list[classify.Failure]:
    """取一轮里的失败用例。``case_outcomes`` 只有结论没有报文，因此 ``message`` 为空——
    归因退化为「按结论类型判定」，这也是把「未知」单列出来的原因之一。"""
    out: list[classify.Failure] = []
    for case in store.run_case_outcomes(run.run_id):
        if case.outcome in ("failed", "error", "xfailed"):
            out.append(classify.Failure(case_id=case.case_id, outcome=case.outcome))
        if len(out) >= limit:
            break
    return out


def _attribution_counts(store: MetricsStore, runs: Sequence[RunRecord]) -> dict[classify.Attribution, int]:
    total: dict[classify.Attribution, int] = {a: 0 for a in classify.Attribution}
    for run in runs[:20]:
        for key, value in classify.summarize(_failures_of(store, run)).items():
            total[key] = total.get(key, 0) + value
    return total


def _total_cases(store: MetricsStore, runs: Sequence[RunRecord]) -> int:
    return sum(run.total for run in runs)


def build_report(
    store: MetricsStore,
    out_dir: str | Path = DEFAULT_REPORT_DIR,
    *,
    coverage: Sequence[OperationCoverage] | None = None,
) -> ReportResult:
    """生成整套 HTML 报表，返回输出目录与各页路径。

    ``coverage`` 不传时从 ``registry build`` 落盘的 JSON 读；JSON 不存在则覆盖页显示
    「没有覆盖数据」而不是伪造一份空矩阵——报表撒谎比报表空白更糟。
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    matrix = list(coverage) if coverage is not None else load_coverage_matrix()

    pages = {
        "index.html": ("qaplat 平台报表", _page_index(store, matrix, directory)),
        "runs.html": ("轮次明细", _page_runs(store)),
        "coverage.html": ("接口覆盖矩阵", _page_coverage(matrix)),
        "attribution.html": ("失败归因", _page_attribution(store)),
        "drift.html": ("契约漂移历史", _page_drift(store)),
    }
    written: list[Path] = []
    for name, (title, body) in pages.items():
        path = directory / name
        path.write_text(_page(title, body), encoding="utf-8", newline="\n")
        written.append(path)

    runs = store.recent_runs(limit=200)
    counts = _attribution_counts(store, runs)
    return ReportResult(
        out_dir=directory,
        pages=tuple(written),
        runs_count=len(runs),
        cases_count=_total_cases(store, runs),
        operations_total=len(matrix),
        covered_total=sum(1 for c in matrix if c.covered_by),
        regressions=counts.get(classify.Attribution.REGRESSION, 0),
    )


def load_attribution_hints(path: str | Path) -> Mapping[str, str]:
    """读可选的失败报文旁证文件（``{nodeid: 报文}``）。

    报表默认只从数据库读结论，因此归因里的环境/数据类证据无法还原。若要更精确的归因，
    在 gate 采集时把报文写成这个 JSON 再传给 report——存在即用，不存在就退化为按结论判定。
    """
    candidate = Path(path)
    if not candidate.exists():
        return {}
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}
