"""独立启动入口：`python -m mock_server.run`。

支持三种用法（pytest 的 subprocess fixture 用的是第一种）：

    # 1) 给 pytest 用：指定随机端口，启动前重置数据库
    python -m mock_server.run --port 8123 --reset

    # 2) 本地手动跑，浏览器打开 http://127.0.0.1:8000/docs 看交互式文档
    python -m mock_server.run

    # 3) 只初始化数据不启动服务（检查种子数据是否正常）
    python -m mock_server.run --init-only

为什么不用 `uvicorn ... --reload`：reload 会 fork 子进程，pytest 结束时
kill 父进程会留下孤儿进程占着端口，下一次跑测试就报 "address already in use"。
这是我们踩过的坑，所以 fixture 里明确不加 --reload。
"""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mock_server.run", description="启动 MES Mock API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--db",
        default=os.getenv("MOCK_DB_PATH"),
        help="SQLite 文件路径，默认为 mock_server/dsh_mes.db",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="启动前删库重建（测试专用：保证每次跑用例的基线数据一致）",
    )
    parser.add_argument("--init-only", action="store_true", help="只初始化数据库后退出")
    parser.add_argument(
        "--concurrency-window-ms",
        type=int,
        default=None,
        help="并发用例的人工窗口毫秒数，默认 120；设为 0 关闭",
    )
    parser.add_argument(
        "--log-level",
        default="warning",
        help="uvicorn 日志级别；用例跑批时保持 warning，否则真实输出会被访问日志埋掉",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.concurrency_window_ms is not None:
        # 必须在 import services 之前设置——模块级读了这个环境变量
        os.environ["MOCK_CONCURRENCY_WINDOW_MS"] = str(args.concurrency_window_ms)

    from mock_server import db

    if args.db:
        db.configure(args.db)
    path = db.init_db(reset=args.reset)

    print(f"[mock_server] 数据库: {path}", flush=True)
    print(f"[mock_server] 表: {', '.join(db.healthcheck()['tables'])}", flush=True)

    if args.init_only:
        from mock_server.db import table_counts

        for table, count in table_counts().items():
            print(f"[mock_server]   {table:<20} {count} 行", flush=True)
        return 0

    import uvicorn

    from mock_server.app import create_app

    app = create_app()
    print(f"[mock_server] 监听 http://{args.host}:{args.port}/docs", flush=True)
    # log_level="warning"：用例输出里不需要 uvicorn 每个请求的访问日志，
    # 否则 pytest 的真实输出会被几千行日志埋掉。
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
