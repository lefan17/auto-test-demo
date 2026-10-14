#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 flaky 治理账本产出两份清单：重跑才通过的用例 / 隔离区用例。

用法::

    python scripts/flaky_report.py                    # 人读的文本
    python scripts/flaky_report.py --markdown         # 贴进 $GITHUB_STEP_SUMMARY
    python scripts/flaky_report.py --json             # 给周报脚本消费
    python scripts/flaky_report.py --ledger other.json

账本由根 ``conftest.py`` 的 ``pytest_sessionfinish`` 钩子写在
``reports/flaky-governance.json``（可用环境变量 ``FLAKY_GOVERNANCE_LEDGER`` 重定向）。

为什么不用 ``--junit-xml`` 当载体：JUnit 的 XML 格式里**没有** rerun 这个概念
（venv 里 ``_pytest/junitxml.py`` 的 ``pytest_runtest_logreport`` 只认
passed/failed/skipped），一次重跑只能靠"同一个 nodeid 出现多个 ``<testcase>``
节点"这种副作用去猜，而那个副作用还会随插件版本变化。治理需要一份能直接读、
字段有名字的账本，所以自己写。

退出码：0 = 账本读到了（无论里面有没有 flaky 记录）；2 = 账本不存在或读不了。
「有重跑」本身不是错误——这里刻意**不**因为出现 flaky 就返回非零，
否则这个 step 会把流水线弄红，而它的定位是观测，不是门禁。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "reports" / "flaky-governance.json"


def load_ledger(path: Path) -> dict[str, Any]:
    """读账本；文件缺失或格式不对时给出可操作的报错，而不是抛栈。"""
    if not path.exists():
        raise SystemExit(
            f"找不到治理账本 {path}\n"
            f"先跑一次 pytest（任意目标都行），根 conftest.py 会在 sessionfinish 时写它。\n"
            f"例如：pytest -m api -n 4 --dist loadfile"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"治理账本不是合法 JSON：{path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"治理账本的顶层结构应为对象：{path}")
    return data


def _nodeid_of(entry: Any) -> str:
    """账本里的条目可能是字符串，也可能是 {"nodeid": ..., "reruns": n}。"""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("nodeid", ""))
    return str(entry)


def render_text(data: dict[str, Any], ledger_path: Path) -> str:
    rerun_passed = [_nodeid_of(e) for e in data.get("rerun_passed", [])]
    rerun_failed = [_nodeid_of(e) for e in data.get("rerun_still_failed", [])]
    quarantined = [_nodeid_of(e) for e in data.get("quarantined", [])]

    lines = [
        f"治理账本: {ledger_path}",
        f"生成时间: {data.get('generated_at', '<未知>')}",
        f"本次调用: {data.get('invocation', '<未知>')}",
        "",
        f"重跑总次数: {data.get('reruns_total', 0)}",
        f"收集用例数: {data.get('collected_count', 0)}",
        "",
        f"== 重跑才通过（{len(rerun_passed)} 条）==",
        "这些用例第一次跑是红的，靠重跑才变绿——它们没有真的通过，只是被抖动挡住了。",
    ]
    lines += [f"  {n}" for n in rerun_passed] or ["  （无）"]

    lines += [
        "",
        f"== 重跑后仍然失败（{len(rerun_failed)} 条）==",
        "抖动之外的失败：重跑没能救回来，就是真问题。",
    ]
    lines += [f"  {n}" for n in rerun_failed] or ["  （无）"]

    lines += [
        "",
        f"== 隔离区（{len(quarantined)} 条）==",
        "明知红/不稳定、暂不删除、已退出主门禁的用例。清单每周都应该被看一眼：",
        "同一条用例连续数周留在这里，就是「红但不修」——用例库死亡的头号原因。",
    ]
    lines += [f"  {n}" for n in quarantined] or ["  （无）"]
    return "\n".join(lines)


def render_markdown(data: dict[str, Any]) -> str:
    rerun_passed = [_nodeid_of(e) for e in data.get("rerun_passed", [])]
    rerun_failed = [_nodeid_of(e) for e in data.get("rerun_still_failed", [])]
    quarantined = [_nodeid_of(e) for e in data.get("quarantined", [])]

    lines = [
        "## flaky 治理",
        "",
        f"- 重跑总次数：**{data.get('reruns_total', 0)}**（本次调用：`{data.get('invocation', '')}`）",
        f"- 重跑才通过：**{len(rerun_passed)}** 条",
        f"- 重跑后仍失败：**{len(rerun_failed)}** 条",
        f"- 隔离区：**{len(quarantined)}** 条",
        "",
    ]

    if rerun_passed:
        lines += [
            "<details><summary>重跑才通过的用例（这些是潜在的 flaky，值得回头看）</summary>",
            "",
        ]
        lines += [f"- `{n}`" for n in rerun_passed]
        lines += ["", "</details>", ""]

    if rerun_failed:
        lines += ["### 重跑后仍然失败", ""]
        lines += [f"- `{n}`" for n in rerun_failed]
        lines += [""]

    lines += ["### 隔离区用例（红但不修 = 用例库死亡的头号原因）", ""]
    if quarantined:
        lines += [f"- `{n}`" for n in quarantined]
    else:
        lines += ["（无）"]
    lines += ["", "> 单独运行隔离区：`pytest -m quarantine`", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 flaky 治理账本：重跑才通过 / 隔离区两份清单。")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help=f"治理账本路径（默认 {DEFAULT_LEDGER}）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--markdown", action="store_true", help="输出 Markdown（贴 $GITHUB_STEP_SUMMARY）")
    group.add_argument("--json", action="store_true", help="原样输出账本 JSON")
    args = parser.parse_args(argv)

    data = load_ledger(args.ledger)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.markdown:
        print(render_markdown(data))
    else:
        print(render_text(data, args.ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
