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
  3. 文档声明的阻塞性 == workflow 里到底有没有 continue-on-error，且**按层级分档**：
     都没有 → 必须写「阻塞」；只有 step 级 → 可写「部分放行」或「不阻塞」；
     有**job 级** → 只能写「不阻塞」。
     分档是因为两者语义不同：job 级豁免让整条 job 的失败都不改变流水线结论（真·非阻塞），
     step 级豁免只放过那一步、job 本身仍会红（顶多算部分放行）。把 job 级漏判成「无豁免」，
     后果是文档写「不阻塞」被误判成错，或者更糟——文档写「阻塞」反而假绿。
     只有 step 级那一档不细分「部分放行/不阻塞」：一个 job 里哪几步算判定步骤，
     只有写策略的人说得清；脚本能客观担保的是「有豁免」这个事实没有被文档漏记。
  4. workflow 里每一条 continue-on-error（job 级与 step 级各算一条），
     都必须在文档的放行清单里逐条登记（双向，多登记也算错）。job 级那条的步骤名写作
     `<job 级>`，因为它并不属于任何一步。
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
# job 级 continue-on-error 是整条 job 的豁免，比 step 级更强：它连 job 自己的红都不算数，
# 所以只接受「不阻塞」这一种写法。
JOB_LEVEL_VALUES = (NOT_BLOCKING,)
# job 级豁免不属于任何一步，在放行清单里用这个固定的伪步骤名登记。
JOB_LEVEL_STEP = "<job 级>"
# 4 空格＝job 级键，8 空格＝step 级键。两者都是真豁免；更深的缩进是 run 块内的脚本内容，
# 不是 YAML 键。整行注释（`#` 开头）在两种缩进下都必须排除——test.yml 的 505/666 行就是
# 靠 `#` 讲 continue-on-error 的注释，把它们当成豁免会让脚本凭空造出两个假门禁。
JOB_LEVEL_COE_RE = re.compile(r" {4}continue-on-error:\s*(\S+)\s*$")
STEP_LEVEL_COE_RE = re.compile(r" {8}continue-on-error:\s*(\S+)\s*$")
# YAML 的行尾注释（`#` 前必须有空白），例如 `continue-on-error: true  # 试点`。
# 不剥掉它，上面两条正则会因为 `$` 对不上而失配——真豁免又会被读成不存在，回到假绿。
TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")


def strip_trailing_comment(raw: str) -> str:
    """去掉 YAML 行尾注释；整行注释返回空串。"""
    if raw.lstrip().startswith("#"):
        return ""
    return TRAILING_COMMENT_RE.sub("", raw)


def is_enabled(value: str) -> bool:
    """这个 continue-on-error 值是不是字面 true——只有它是，才算真的放行。

    `continue-on-error: false` 与表达式（`${{ ... }}`）都不能当作豁免：
    前者语义上就是「阻断」，后者脚本无法静态判定。把它们算成豁免会让
    「文档写不阻塞」凭空通过，是同一类假绿。
    """
    return value.strip().lower() == "true"


@dataclass(frozen=True)
class WorkflowJob:
    """workflow 里一个 job 的客观事实，全部来自文件本身。"""

    name: str
    line: int
    timeout: int | None
    relaxed_steps: tuple[tuple[str, str, int], ...]  # (步骤名, continue-on-error 值, 行号)
    relaxed_job: tuple[str, int] | None  # job 级 continue-on-error：(值, 行号)；没有则 None

    @property
    def relaxed_entries(self) -> tuple[tuple[str, str, int], ...]:
        """这个 job 的全部豁免，job 级排在前面（它比任何 step 级豁免都强）。"""
        entries: list[tuple[str, str, int]] = []
        if self.relaxed_job is not None:
            value, line = self.relaxed_job
            entries.append((JOB_LEVEL_STEP, value, line))
        entries.extend(self.relaxed_steps)
        return tuple(entries)

    @property
    def blocking_choices(self) -> tuple[str, ...]:
        """这个 job 的豁免形态允许文档写哪几种阻塞性。"""
        if self.relaxed_job is not None:
            return JOB_LEVEL_VALUES
        return RELAXED_VALUES if self.relaxed_steps else (BLOCKING,)

    @property
    def exemptions_text(self) -> str:
        """给报错信息用的豁免清单描述。"""
        return "、".join(f"第 {line} 行 `{step}`" for step, _, line in self.relaxed_entries) or "无"


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
    """按缩进解析 job、job 级 timeout、job 级与 step 级的 continue-on-error。

    刻意不 import yaml：这份 workflow 只有 job/step 两层结构，而 YAML 1.1 会把顶格的
    `on:` 解析成布尔 True、还要为此写补偿代码；缩进扫描反而更直白，也让本脚本零依赖。

    豁免有两种缩进，必须都认：job 级（4 空格）与 step 级（8 空格）。只认后者会让 job 级
    豁免隐形，于是文档写「不阻塞」被误判、写「阻塞」反而假绿。
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
        relaxed_job: tuple[str, int] | None = None
        step_name = "<步骤外>"
        for offset, raw in enumerate(lines[line_no - 1 : stop]):
            current = line_no + offset
            body = strip_trailing_comment(raw)
            if not body:
                continue
            named = re.fullmatch(r" {6}- name:\s*(.+)", body)
            if named:
                step_name = named.group(1).strip()
                continue
            if re.fullmatch(r" {6}- .*", body):
                step_name = "<未命名步骤>"
                continue
            if timeout is None:
                found = re.fullmatch(r" {4}timeout-minutes:\s*(\d+)", body)
                if found:
                    timeout = int(found.group(1))
            job_level = JOB_LEVEL_COE_RE.fullmatch(body)
            if job_level:
                # 只有字面 true 才是豁免；false 与表达式都不是，见 is_enabled。
                relaxed_job = (job_level.group(1), current) if is_enabled(job_level.group(1)) else None
                continue
            step_level = STEP_LEVEL_COE_RE.fullmatch(body)
            if step_level and is_enabled(step_level.group(1)):
                relaxed.append((step_name, step_level.group(1), current))
        jobs[name] = WorkflowJob(
            name=name,
            line=line_no,
            timeout=timeout,
            relaxed_steps=tuple(relaxed),
            relaxed_job=relaxed_job,
        )
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
        expected = job.blocking_choices
        if doc.blocking not in expected:
            if job.relaxed_job is not None:
                found = f"workflow 有 job 级 continue-on-error（{job.exemptions_text}）"
            elif job.relaxed_steps:
                found = f"workflow 有 step 级 continue-on-error（{job.exemptions_text}）"
            else:
                found = "workflow 里没有任何 continue-on-error"
            report.bad(
                f"{name} 阻塞性",
                f"文档第 {doc.line} 行写「{doc.blocking}」，但 {found} → 应写「{' 或 '.join(expected)}」",
            )
        else:
            report.ok(
                f"{name} 阻塞性",
                f"文档「{doc.blocking}」→ workflow 豁免 {len(job.relaxed_entries)} 条"
                f"{'（含 job 级）' if job.relaxed_job is not None else ''}",
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
        (job.name, step): (value, line) for job in workflow_jobs.values() for step, value, line in job.relaxed_entries
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
