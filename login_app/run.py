"""独立启动入口：`python -m login_app.run`。

用法与 mock_server/run.py 保持一致（pytest 的 fixture 用的是第一种）：

    # 1) 给 pytest 用：随机端口
    python -m login_app.run --port 8123

    # 2) 本地手动跑，浏览器打开 http://127.0.0.1:5001/docs 看交互式文档
    python -m login_app.run --port 5001

同样明确**不加 --reload**：reload 会 fork 子进程，pytest 结束时 kill 父进程
会留下孤儿占着端口，下一次跑用例就报 "address already in use"。
这个坑 MES 那边已经踩过一次，这里不重复踩。
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="login_app.run", description="启动登录服务（被测系统）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument(
        "--log-level",
        default="warning",
        help="uvicorn 日志级别；跑批时保持 warning，否则真实输出会被访问日志埋掉",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import uvicorn

    from login_app.app import create_app

    app = create_app()
    print(f"[login_app] 监听 http://{args.host}:{args.port}/docs", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
