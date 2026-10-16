"""四层门禁：表达式拼装、pytest 调用、阈值判定。

四层的划分依据是「谁来等这个结果」：

- ``smoke``   提交前跑，1 分钟内必须出结论 → 表达式 ``smoke``
- ``pr``      合并请求上跑 → ``smoke or api or platform``
- ``nightly`` 每晚跑全量非 UI → ``not ui``
- ``release`` 发版前跑全量（含 UI）→ 空表达式

**通过率分母是 passed + failed**（可判定用例），不含 skipped / error / xfailed。
理由是 ``error`` 属于「没跑成」而不是「跑失败」：本机 smoke 层的 6 条 UI 用例因沙箱里
没有浏览器而 error，把这种环境故障算进分母，会让 smoke 门禁常年红灯，红灯一多就没人看了。
环境问题不隐藏——它照样落库进 ``errors`` 字段、进 report 的归因页，只是不冒充质量信号。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from qaplat.metrics import RunRecord, pass_rate_of, utc_now
from qaplat.paths import CACHE_DIR, ROOT

#: 门禁把每轮原始采集写在这里，供 report / registry 复用同一份数据。
RAW_DUMP_DIR = CACHE_DIR / "raw"


def _dump_path_for(layer: str) -> Path:
    return RAW_DUMP_DIR / f"{layer}.json"


#: 采集插件以 ``-p`` 加载；``qaplat._dump`` 必须可从 cwd（仓库根）导入。
DUMP_PLUGIN = "qaplat._dump"


class Layer(StrEnum):
    """四层门禁。"""

    SMOKE = "smoke"
    PR = "pr"
    NIGHTLY = "nightly"
    RELEASE = "release"


@dataclass(frozen=True)
class LayerSpec:
    layer: Layer
    marker_expr: str
    timeout_seconds: int
    min_pass_rate: float
    max_duration_seconds: int
    max_errors: int


#: 四层定义。``marker_expr`` 由 FROZEN_CLI.md 固定，不得自创 marker。
#:
#: 阈值取舍：
#: - smoke / pr 要求 ``min_pass_rate = 1.0``：这两层的意义就是「一条都不许红」。
#: - nightly 0.98 / release 0.95：保留少量容忍度，把「已知缺陷探针」这类长期红灯
#:   挡在阻断线之外；真正想阻断的回归仍会通过 report 的归因页暴露出来。
#: - ``max_duration_seconds`` 取目标时长的 1.5 倍左右，超了算预算违规但不伪装成失败。
#: - ``max_errors``（2026-10 补的第五条 blocking 判据）：阻塞层一条都不许 error，
#:   全量层留少量环境抖动余量（浏览器/网络故障不属于被测回归）。它堵的缺口是
#:   「没跑成的用例被静默当成通过」——见 ``evaluate()`` 里 error 判据必须独立的原因。
LAYERS: dict[Layer, LayerSpec] = {
    Layer.SMOKE: LayerSpec(
        layer=Layer.SMOKE,
        marker_expr="smoke",
        timeout_seconds=600,
        min_pass_rate=1.0,
        max_duration_seconds=90,
        max_errors=0,
    ),
    Layer.PR: LayerSpec(
        layer=Layer.PR,
        marker_expr="smoke or api or platform",
        timeout_seconds=900,
        min_pass_rate=1.0,
        max_duration_seconds=450,
        max_errors=0,
    ),
    Layer.NIGHTLY: LayerSpec(
        layer=Layer.NIGHTLY,
        marker_expr="not ui",
        timeout_seconds=1800,
        min_pass_rate=0.98,
        max_duration_seconds=1200,
        max_errors=3,
    ),
    Layer.RELEASE: LayerSpec(
        layer=Layer.RELEASE,
        marker_expr="",
        timeout_seconds=2700,
        min_pass_rate=0.95,
        max_duration_seconds=2400,
        max_errors=5,
    ),
}


@dataclass(frozen=True)
class GateResult:
    layer: Layer
    run_id: str
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    duration_seconds: float
    pass_rate: float
    within_budget: bool
    violations: tuple[str, ...]


@dataclass(frozen=True)
class PytestRun:
    """一次 pytest 子进程的原始产出。"""

    exit_code: int
    duration_seconds: float
    counts: dict[str, int]
    cases: tuple[tuple[str, str, float], ...]
    markers: dict[str, tuple[str, ...]]
    files: dict[str, tuple[str, ...]]
    messages: dict[str, str] = field(default_factory=dict)


def build_pytest_args(spec: LayerSpec, extra: Sequence[str] = ()) -> list[str]:
    """按层拼出 pytest 命令行参数。

    顺序固定为「表达式 → 采集插件 → 调用方附加参数」，``extra`` 放最后是为了让调用方
    能覆盖前面的设置（pytest 后出现的同名选项生效，例如 ``--`` 之后的用例路径）。
    空表达式（release 层）不产生 ``-m ""``——pytest 对空 ``-m`` 的语义是把全部用例
    标记成匹配，多写反而容易误解。
    """
    args: list[str] = []
    if spec.marker_expr:
        args += ["-m", spec.marker_expr]
    args += ["-p", DUMP_PLUGIN, "--qaplat-dump", str(_dump_path_for(spec.layer.value))]
    args += [str(item) for item in extra]
    return args


_SUMMARY_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|deselected|warning|warnings)")
_SUMMARY_SECONDS_RE = re.compile(r"in\s+([\d.]+)s")


def parse_summary_line(text: str) -> dict[str, int]:
    """从 pytest 结尾汇总行解析计数，形如 ``30 passed, 327 deselected, 6 errors in 24.29s``。

    只在采集插件缺失（没拿到 ``--qaplat-dump`` 产物）时作为兜底；正常路径用插件数据，
    因为它能区分 error 与 failed，而汇总行里两者都只是计数。
    """
    counts: dict[str, int] = {}
    for match in _SUMMARY_COUNT_RE.finditer(text):
        name = match.group(2)
        if name in ("error", "errors"):
            name = "error"
        elif name in ("warning", "warnings"):
            continue
        counts[name] = counts.get(name, 0) + int(match.group(1))
    return counts


def parse_summary_seconds(text: str) -> float | None:
    """从汇总行取本轮耗时（秒）。"""
    match = _SUMMARY_SECONDS_RE.search(text)
    return float(match.group(1)) if match else None


def run_layer(
    spec: LayerSpec,
    extra: Sequence[str] = (),
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> PytestRun:
    """在子进程里跑一层，返回原始产出。

    ``cwd`` 默认仓库根：pytest.ini 与 conftest.py 都按 cwd 解析，跑偏了 testpaths 就失效。
    """
    workdir = cwd or ROOT
    dump = _dump_path_for(spec.layer.value)
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.unlink(missing_ok=True)

    argv = [sys.executable, "-m", "pytest", *build_pytest_args(spec, extra)]
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_seconds,
            check=False,
        )
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        return PytestRun(
            exit_code=EXIT_TIMEOUT,
            duration_seconds=elapsed,
            counts={"timeout": 1},
            cases=(),
            markers={},
            files={},
        )
    elapsed = time.monotonic() - started

    payload = _read_dump(dump)
    if payload is None:
        counts = parse_summary_line(stdout)
        summary_seconds = parse_summary_seconds(stdout)
        if summary_seconds is not None:
            elapsed = summary_seconds
        counts.setdefault("total", sum(counts.get(k, 0) for k in ("passed", "failed", "error", "skipped", "xfailed")))
        if not payload and stderr.strip():
            counts["_stderr"] = 1
        return PytestRun(
            exit_code=exit_code,
            duration_seconds=elapsed,
            counts=counts,
            cases=(),
            markers={},
            files={},
        )

    raw_cases = payload.get("cases")
    case_rows = raw_cases if isinstance(raw_cases, list) else []
    cases = tuple(
        (str(item["nodeid"]), str(item["outcome"]), float(item.get("duration_seconds") or 0.0))
        for item in case_rows
        if isinstance(item, dict) and "nodeid" in item and "outcome" in item
    )
    counts = _counts_from_cases(cases)
    messages = {
        str(item["nodeid"]): str(item.get("message") or "")
        for item in case_rows
        if isinstance(item, dict) and "nodeid" in item and item.get("message")
    }
    raw_markers = payload.get("markers")
    marker_map = raw_markers if isinstance(raw_markers, dict) else {}
    raw_files = payload.get("files")
    file_map = raw_files if isinstance(raw_files, dict) else {}
    return PytestRun(
        exit_code=exit_code,
        duration_seconds=elapsed,
        counts=counts,
        cases=cases,
        markers={str(k): tuple(str(m) for m in v) for k, v in marker_map.items() if isinstance(v, list)},
        files={str(k): (str(v),) for k, v in file_map.items()},
        messages=messages,
    )


#: 子进程超时在 pytest 自己的退出码体系之外，用 124（``timeout(1)`` 的约定）表示。
EXIT_TIMEOUT = 124


def _counts_from_cases(cases: Sequence[tuple[str, str, float]]) -> dict[str, int]:
    counts = {name: 0 for name in ("passed", "failed", "error", "skipped", "xfailed")}
    for _nodeid, outcome, _duration in cases:
        if outcome in counts:
            counts[outcome] += 1
    counts["total"] = len(cases)
    return counts


def _read_dump(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def evaluate(spec: LayerSpec, run: RunRecord) -> GateResult:
    """把一轮结果与层阈值比对。

    ``violations`` 只装**阻断性**违规，空元组即达标——CLI 的退出码直接由它决定，
    不做第二套判断逻辑。非阻断的诊断信息（例如归因分类）不进这里，进 report；
    但 error 超预算属于**第五条阻断判据**，它故意留在 violations 里。
    """
    violations: list[str] = []
    decisive = run.passed + run.failed
    if run.total == 0:
        violations.append("该层未收集到任何用例（可能是 marker 拼错或 testpaths 失效）")
    elif decisive == 0:
        violations.append(f"该层 {run.total} 条用例全部未产出可判定结论（skipped/error），通过率无法计算")
    elif run.pass_rate < spec.min_pass_rate:
        violations.append(
            f"通过率 {run.pass_rate:.1%} 低于阈值 {spec.min_pass_rate:.1%}"
            f"（passed={run.passed} failed={run.failed} total={run.total}）"
        )

    # error 预算**必须独立判定，不能挂进上面的 else-if 链**。
    # 理由：分母 ``decisive = passed + failed`` 不含 error，实测 smoke 层
    # ``total=36 passed=30 failed=0 errors=6`` 会算出 ``decisive=30``、``pass_rate=1.0``。
    # 一旦把它接成 ``elif``，该轮会先被 pass_rate 分支判为达标，``run.errors`` 永远轮不到检查，
    # 「没跑成的用例」就被静默当成通过。所以这里另起一条独立判据。
    if run.errors > spec.max_errors:
        violations.append(f"error 数 {run.errors} 超出预算 {spec.max_errors}（没跑成的用例不计入通过率，需单独阻塞）")

    within_budget = run.duration_seconds <= spec.max_duration_seconds
    if not within_budget:
        violations.append(f"耗时 {run.duration_seconds:.1f}s 超出预算 {spec.max_duration_seconds}s")

    return GateResult(
        layer=spec.layer,
        run_id=run.run_id,
        total=run.total,
        passed=run.passed,
        failed=run.failed,
        errors=run.errors,
        skipped=run.skipped,
        xfailed=run.xfailed,
        duration_seconds=run.duration_seconds,
        pass_rate=run.pass_rate,
        within_budget=within_budget,
        violations=tuple(violations),
    )


def exit_code_for(result: GateResult) -> int:
    """门禁结论 → CLI 退出码：0 达标 / 1 未达标。用法错误由 CLI 自己返回 2。"""
    return 0 if not result.violations else 1


def run_record_from(
    spec: LayerSpec,
    run: PytestRun,
    *,
    run_id: str,
    commit_sha: str | None = None,
    started_at: datetime | None = None,
) -> RunRecord:
    """把子进程产出转成可落库的 ``RunRecord``。"""
    counts = run.counts
    passed = int(counts.get("passed", 0))
    failed = int(counts.get("failed", 0))
    errors = int(counts.get("error", 0))
    total = int(counts.get("total", passed + failed + errors))
    return RunRecord(
        run_id=run_id,
        layer=spec.layer.value,
        commit_sha=commit_sha,
        started_at=started_at or utc_now(),
        duration_seconds=run.duration_seconds,
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=int(counts.get("skipped", 0)),
        xfailed=int(counts.get("xfailed", 0)),
        pass_rate=pass_rate_of(passed, failed, total),
        outcome=("error" if total and not (passed or failed) else ("failed" if failed or errors else "passed")),
    )
