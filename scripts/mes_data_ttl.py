#!/usr/bin/env python
"""清理 MES 库里的历史测试数据（TTL 入口）。

为什么要有这个脚本（而不是只留一个测试）：
会话级 fixture 每次都新建一份库，所以**跨会话残留只发生在手工/CI 共用的那份库**上
（默认 `mock_server/dsh_mes.db`）。那种残留没有任何进程还在负责它 ——
teardown 早随崩溃的会话一起没了。这个脚本就是那把扫帚。

安全默认：
- 不带 `--apply` 时是**试运行**，只报告将删什么，一行都不动；
- 基线数据（种子用户/物料/库存）有硬白名单，命中也永不删除；
- 判定不出来的时间戳按"未过期"处理 —— 宁可留着被人看到，也不猜着删。

用法：
    python scripts/mes_data_ttl.py                      # 干跑，看会删什么（TTL 24h）
    python scripts/mes_data_ttl.py -n 1                 # 换个 TTL：超过 1 小时
    python scripts/mes_data_ttl.py --apply              # 真的删
    python scripts/mes_data_ttl.py --apply --json       # 给 CI 消费的结构化输出
    python scripts/mes_data_ttl.py --list-lingering     # 只看非基线残留（不看时间）
    python scripts/mes_data_ttl.py --fail-on-lingering  # 清完仍有残留则退出码 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MES_DIR = ROOT / "testcases" / "mes"
if str(MES_DIR) not in sys.path:
    sys.path.insert(0, str(MES_DIR))

from data_ttl import (  # noqa: E402 - 路径注入后才能导入
    DEFAULT_TTL_HOURS,
    format_report,
    lingering_rows,
    purge,
)


def default_db_path() -> Path:
    from mock_server import db as mock_db

    return Path(mock_db.get_default_db_path())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mes_data_ttl.py",
        description="清理超过 TTL 的历史 MES 测试数据（默认试运行，基线数据受白名单保护）。",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="目标 SQLite 库；默认 mock_server/dsh_mes.db（那份手工/CI 共用库）。",
    )
    parser.add_argument(
        "-n",
        "--ttl-hours",
        type=float,
        default=DEFAULT_TTL_HOURS,
        help=f"过期阈值，单位小时（默认 {DEFAULT_TTL_HOURS:g}）。",
    )
    parser.add_argument("--apply", action="store_true", help="真的执行删除；不加则只试运行。")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于 CI/脚本消费。")
    parser.add_argument("--list-lingering", action="store_true", help="只列出非基线残留行（忽略 TTL），不删任何东西。")
    parser.add_argument("--fail-on-lingering", action="store_true", help="结束时仍有非基线残留则以退出码 3 失败。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db or default_db_path()

    if args.list_lingering:
        problems = lingering_rows(db_path)
        if args.json:
            print(json.dumps({"db": str(db_path), "lingering": problems}, ensure_ascii=False, indent=2))
        else:
            print(f"[数据 TTL] {db_path} 的非基线残留：")
            print("\n".join(problems) if problems else "  无")
        return 3 if (problems and args.fail_on_lingering) else 0

    report = purge(db_path, args.ttl_hours, apply=args.apply)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))

    if args.fail_on_lingering and report.lingering:
        print(f"[数据 TTL] 清理后仍有 {len(report.lingering)} 处非基线残留（超期之外的），退出码 3。", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
