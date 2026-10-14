#!/usr/bin/env python
"""门禁策略一致性校验：pytest.ini 的 marker、test.yml 的 job 与放行步骤、策略文档，三者必须说同一件事。

为什么需要它：策略一旦写进文档，判据却仍然只活在 workflow 的 `continue-on-error` 上——
文档说 `security` 不拦，真正让它不拦的是那行 YAML。两边靠人眼对照，第一次有人改 workflow 就会分叉，
分叉之后「策略」重新变成没人知道、也没人敢动的默会知识。
这个脚本把「文档声明」与「workflow 事实」逐条对上，任一条对不上就非零退出，
让「文档过时」变成一次红灯，而不是一次口头提醒。

判定规则（全部读文件本身，不解析人写的注释，也不依赖 YAML 库）：
  1. 文档的 job 集合 == workflow 的 job 集合，多一个少一个都算错。
  2. 文档声明的 job 超时 == workflow 的 timeout-minutes。
  3. 文档声明的阻塞性 == workflow 里到底有没有 continue-on-error 步骤：
     没有 → 必须写「阻塞」；有 → 必须写「部分放行」或「不阻塞」。
     这里刻意不区分「部分放行」与「不阻塞」：一个 job 里哪几步算判定步骤，
     只有写策略的人说得清；脚本能客观担保的是「有放行步骤」这个事实没有被文档漏记。
  4. workflow 里每一个 continue-on-error 步骤，都必须在文档的放行清单里逐条登记（双向，多登记也算错）。
  5. 文档的 marker 表覆盖 pytest.ini 声明的全部 marker（双向，写多一个也是错）。

用法::

    python scripts/check_gate_policy.py        # 打印对照表；不一致时退出 1
    python scripts/check_gate_policy.py -v     # 逐条打印 marker 的归属行号

退出码：0 = 三者一致；1 = 有对不上的条目（逐条打印对不上的位置与两边原文）。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTEST_INI = REPO_ROOT / "pytest.ini"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
POLICY_DOC = REPO_ROOT / "docs" / "质量门禁策略.md"

# 策略文档里三段机器可读表格的 HTML 注释锚点。用注释而不是标题文字做锚点：
# 标题会被人顺手改掉，改完之后脚本静默找不到段落就退化成"什么都没检查"。
JOB_SECTION = "gate-policy:jobs"
RELAXED_SECTION = "gate-policy:continue-on-error"
MARKER_SECTION = "gate-policy:markers"

BLOCKING = "阻塞"
PARTIAL = "部分放行"
NOT_BLOCKING = "不阻塞"
RELAXED_VALUES = (PARTIAL, NOT_BLOCKING)


@dataclass(frozen=True)
class WorkflowJob:
    """workflow 里一个 job 的客观事实，全部来自文件本身。"""

    name: str
    line: int
    timeout: int | None
    relaxed_steps: tuple[tuple[str, str, int], ...]  # (步骤名, continue-on-error 值, 行号)


@dataclass(frozen=True)
class DocJob:
    name: str
    line: int
    blocking: str
    timeout: int | None


@dataclass(frozen=True)
class DocRelaxed:
    job: str
    step: str
    value: str
    line: int


@dataclass
class Report:
    """逐条打印对照结论，同时累计失败条目。"""

    failures: list[str] = field(default_factory=list)

    def ok(self, label: str, detail: str) -> None:
        print(f"  PASS  {label} | {detail}")

    def bad(self, label: str, detail: str) -> None:
        print(f"  FAIL  {label} | {detail}")
        self.failures.append(f"{label}（{detail}）")


def read_text(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"找不到文件：{path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 三个真值来源的解析
# ---------------------------------------------------------------------------
def parse_pytest_markers(text: str) -> dict[str, int]:
    """取 `markers =` 块里每个 `name: 说明` 的 marker 名 → 行号。

    块内 marker 的说明可以是多行（缩进更深），只认缩进恰好 4 空格且以 `名字:` 开头的那一行；
    块在第一个不缩进的行（`log_cli` 之类）结束。
    """
    markers: dict[str, int] = {}
    inside = False
    for line_no, raw in enumerate(text.splitlines(), 1):
        if re.fullmatch(r"markers\s*=\s*", raw):
            inside = True
            continue
        if not inside:
            continue
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            break
        found = re.fullmatch(r" {4}([a-z_][a-z0-9_]*):.*", raw)
        if found:
            markers[found.group(1)] = line_no
    if not markers:
        raise SystemExit(f"{PYTEST_INI} 里没解析到任何 marker")
    return markers


def parse_workflow(text: str) -> dict[str, WorkflowJob]:
    """按缩进解析 job、job 级 timeout 与带 continue-on-error 的步骤。

    刻意不 import yaml：这份 workflow 只有 job/step 两层结构，而 YAML 1.1 会把顶格的
    `on:` 解析成布尔 True、还要为此写补偿代码；缩进扫描反而更直白，也让本脚本零依赖。
    """
    lines = text.splitlines()
    try:
        jobs_line = next(i for i, raw in enumerate(lines, 1) if raw.rstrip() == "jobs:")
    except StopIteration:
        raise SystemExit(f"{WORKFLOW} 里找不到顶格的 `jobs:`") from None

    starts: list[tuple[int, str]] = []
    for line_no in range(jobs_line + 1, len(lines) + 1):
        found = re.fullmatch(r" {2}([A-Za-z0-9_-]+):", lines[line_no - 1])
        if found:
            starts.append((line_no, found.group(1)))
    if not starts:
        raise SystemExit(f"{WORKFLOW} 的 `jobs:` 下面没解析到任何 job")

    jobs: dict[str, WorkflowJob] = {}
    for position, (line_no, name) in enumerate(starts):
        stop = starts[position + 1][0] - 1 if position + 1 < len(starts) else len(lines)
        timeout: int | None = None
        relaxed: list[tuple[str, str, int]] = []
        step_name = "<步骤外>"
        for offset, raw in enumerate(lines[line_no - 1 : stop]):
            current = line_no + offset
            named = re.fullmatch(r" {6}- name:\s*(.+)", raw)
            if named:
                step_name = named.group(1).strip()
                continue
            if re.fullmatch(r" {6}- .*", raw):
                step_name = "<未命名步骤>"
                continue
            if timeout is None:
                found = re.fullmatch(r" {4}timeout-minutes:\s*(\d+)", raw)
                if found:
                    timeout = int(found.group(1))
            coefficient = re.fullmatch(r" {8}continue-on-error:\s*(\S+)", raw)
            if coefficient:
                relaxed.append((step_name, coefficient.group(1), current))
        jobs[name] = WorkflowJob(name=name, line=line_no, timeout=timeout, relaxed_steps=tuple(relaxed))
    return jobs


def section_rows(text: str, section: str) -> list[tuple[int, list[str]]]:
    """取 `<!-- section -->` 与 `<!-- /section -->` 之间的 Markdown 表格数据行。"""
    lines = text.splitlines()
    start: int | None = None
    end: int | None = None
    for line_no, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if stripped == f"<!-- {section} -->":
            start = line_no
        elif stripped == f"<!-- /{section} -->" and start is not None:
            end = line_no
            break
    if start is None or end is None:
        raise SystemExit(f"策略文档里找不到机器可读段 <!-- {section} --> … <!-- /{section} -->")

    rows: list[tuple[int, list[str]]] = []
    for line_no in range(start + 1, end):
        stripped = lines[line_no - 1].strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or set(cells[0]) <= set("-: "):
            continue
        if cells[0].strip("`") in ("job", "marker"):
            continue
        rows.append((line_no, cells))
    return rows


def cell(cells: list[str], index: int, line_no: int, section: str) -> str:
    if index >= len(cells):
        raise SystemExit(f"策略文档第 {line_no} 行（{section} 段）缺第 {index + 1} 列")
    return cells[index]


def code(cell_text: str) -> str:
    """表格里被反引号包住的名字取内层，没包就原样返回。"""
    found = re.fullmatch(r"`([^`]+)`", cell_text)
    return found.group(1) if found else cell_text


def parse_doc_jobs(text: str) -> dict[str, DocJob]:
    jobs: dict[str, DocJob] = {}
    for line_no, cells in section_rows(text, JOB_SECTION):
        timeout_text = cell(cells, 2, line_no, JOB_SECTION)
        jobs[code(cell(cells, 0, line_no, JOB_SECTION))] = DocJob(
            name=code(cells[0]),
            line=line_no,
            blocking=cell(cells, 1, line_no, JOB_SECTION),
            timeout=int(timeout_text) if timeout_text.isdigit() else None,
        )
    if not jobs:
        raise SystemExit(f"策略文档的 {JOB_SECTION} 段是空的")
    return jobs


def parse_doc_relaxed(text: str) -> list[DocRelaxed]:
    relaxed: list[DocRelaxed] = []
    for line_no, cells in section_rows(text, RELAXED_SECTION):
        relaxed.append(
            DocRelaxed(
                job=code(cell(cells, 0, line_no, RELAXED_SECTION)),
                step=code(cell(cells, 1, line_no, RELAXED_SECTION)),
                value=cell(cells, 2, line_no, RELAXED_SECTION),
                line=line_no,
            )
        )
    return relaxed


def parse_doc_markers(text: str) -> dict[str, int]:
    markers: dict[str, int] = {}
    for line_no, cells in section_rows(text, MARKER_SECTION):
        markers[code(cell(cells, 0, line_no, MARKER_SECTION))] = line_no
    if not markers:
        raise SystemExit(f"策略文档的 {MARKER_SECTION} 段是空的")
    return markers


# ---------------------------------------------------------------------------
# 对照
# ---------------------------------------------------------------------------
def check_job_sets(workflow_jobs: dict[str, WorkflowJob], doc_jobs: dict[str, DocJob], report: Report) -> None:
    print("== ① job 集合 ==")
    for name in sorted(set(workflow_jobs) | set(doc_jobs)):
        if name not in doc_jobs:
            report.bad(
                f"job {name}",
                f"workflow 第 {workflow_jobs[name].line} 行有它，策略文档的 job 表里没有——新增 job 必须同时登记策略",
            )
        elif name not in workflow_jobs:
            report.bad(
                f"job {name}",
                f"策略文档第 {doc_jobs[name].line} 行声明了它，但 workflow 里已经没有这个 job",
            )
        else:
            report.ok(f"job {name}", f"两边都有（workflow:{workflow_jobs[name].line} / 文档:{doc_jobs[name].line}）")


def check_blocking(workflow_jobs: dict[str, WorkflowJob], doc_jobs: dict[str, DocJob], report: Report) -> None:
    print("\n== ② 阻塞性与超时 ==")
    for name in sorted(set(workflow_jobs) & set(doc_jobs)):
        job = workflow_jobs[name]
        doc = doc_jobs[name]
        expected = RELAXED_VALUES if job.relaxed_steps else (BLOCKING,)
        if doc.blocking not in expected:
            relaxed_text = "、".join(f"第 {line} 行 `{step}`" for step, _, line in job.relaxed_steps) or "无"
            report.bad(
                f"{name} 阻塞性",
                f"文档第 {doc.line} 行写「{doc.blocking}」，但 workflow 里{'有' if job.relaxed_steps else '没有'} "
                f"continue-on-error（{relaxed_text}）→ 应写「{' 或 '.join(expected)}」",
            )
        else:
            report.ok(
                f"{name} 阻塞性",
                f"文档「{doc.blocking}」→ workflow 放行步骤 {len(job.relaxed_steps)} 个",
            )
        if doc.timeout != job.timeout:
            report.bad(
                f"{name} 超时",
                f"文档第 {doc.line} 行 {doc.timeout} 分钟，workflow 第 {job.line} 行 {job.timeout} 分钟",
            )
        else:
            report.ok(f"{name} 超时", f"{doc.timeout} 分钟（workflow:{job.line}）")


def check_relaxed_steps(workflow_jobs: dict[str, WorkflowJob], doc_relaxed: list[DocRelaxed], report: Report) -> None:
    print("\n== ③ continue-on-error 放行清单 ==")
    actual = {
        (job.name, step): (value, line) for job in workflow_jobs.values() for step, value, line in job.relaxed_steps
    }
    declared = {(item.job, item.step): item for item in doc_relaxed}

    for key in sorted(set(actual) | set(declared)):
        label = f"{key[0]} / {key[1]}"
        if key not in declared:
            value, line = actual[key]
            report.bad(label, f"workflow 第 {line} 行是 continue-on-error: {value}，策略文档的放行清单里没有登记")
        elif key not in actual:
            report.bad(label, f"策略文档第 {declared[key].line} 行登记了它，但 workflow 里没有这一步（或它已不再放行）")
        else:
            item = declared[key]
            value, line = actual[key]
            if item.value != value:
                report.bad(label, f"文档第 {item.line} 行写 {item.value}，workflow 第 {line} 行是 {value}")
            else:
                report.ok(label, f"{item.value}（workflow:{line} / 文档:{item.line}）")


def check_markers(markers: dict[str, int], doc_markers: dict[str, int], report: Report, verbose: bool) -> None:
    print("\n== ④ marker 归属 ==")
    for name in sorted(set(markers) | set(doc_markers)):
        if name not in doc_markers:
            report.bad(
                f"marker {name}",
                f"pytest.ini 第 {markers[name]} 行声明了它，策略文档的 marker 表里没有归属——这个 marker 没人管",
            )
        elif name not in markers:
            report.bad(
                f"marker {name}",
                f"策略文档第 {doc_markers[name]} 行声明了它，但 pytest.ini 里没有这个 marker",
            )
        elif verbose:
            report.ok(f"marker {name}", f"pytest.ini:{markers[name]} → 文档:{doc_markers[name]}")
    if not verbose and set(markers) <= set(doc_markers):
        report.ok("marker 覆盖", f"{len(markers)} 个 marker 全部有归属，且没有多余条目")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 docs/质量门禁策略.md 与 test.yml / pytest.ini 是否一致。")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印 marker 与 job 的归属行号")
    args = parser.parse_args(argv)

    workflow_jobs = parse_workflow(read_text(WORKFLOW))
    markers = parse_pytest_markers(read_text(PYTEST_INI))
    doc_text = read_text(POLICY_DOC)
    doc_jobs = parse_doc_jobs(doc_text)
    doc_relaxed = parse_doc_relaxed(doc_text)
    doc_markers = parse_doc_markers(doc_text)

    print(f"[gate-policy] workflow job：{'、'.join(workflow_jobs)}")
    print(f"[gate-policy] pytest.ini marker（{len(markers)} 个）：{'、'.join(markers)}")
    print(f"[gate-policy] 策略文档：{POLICY_DOC.relative_to(REPO_ROOT)}\n")

    report = Report()
    check_job_sets(workflow_jobs, doc_jobs, report)
    check_blocking(workflow_jobs, doc_jobs, report)
    check_relaxed_steps(workflow_jobs, doc_relaxed, report)
    check_markers(markers, doc_markers, report, args.verbose)

    print()
    if report.failures:
        print(f"[gate-policy] 不一致 {len(report.failures)} 项：")
        for failure in report.failures:
            print(f"  - {failure}")
        print("[gate-policy] 策略文档与 workflow / pytest.ini 已经分叉，请让两边重新说同一件事。")
        return 1
    print("[gate-policy] 通过：策略文档、test.yml、pytest.ini 三者一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
