"""独立启动入口：`python -m mes_web.run --host 127.0.0.1 --port <N> --mock-base-url <URL>`。

`--mock-base-url` 必填，且指的是 mock_server 的**根** URL（形如 `http://127.0.0.1:54321`），
**不含** `/api/v1`——前缀由 `mes_web.app` 在拼接时补上。传成 `MockServer.base_url`
（已经带了 `/api/v1`）会让所有转发变成 `/api/v1/api/v1/...` 并 404；
`/healthz` 会原样回显这个值，`_wait_healthy` 靠核对它来抓这种错配。

和 `mock_server/run.py` 一样不加 `--reload`：reload 会 fork 子进程，pytest 结束时 kill 父进程
会留下孤儿占端口，下一次跑就报 "address already in use"。
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mes_web.run", description="启动 MES Demo Web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--mock-base-url",
        required=True,
        help="mock_server 的根 URL（不含 /api/v1），例如 http://127.0.0.1:54321",
    )
    parser.add_argument(
        "--log-level",
        default="warning",
        help="uvicorn 日志级别；用例跑批时保持 warning，否则真实输出会被访问日志埋掉",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import uvicorn

    from mes_web.app import create_app

    app = create_app(args.mock_base_url)
    print(f"[mes_web] mock_server 根 URL: {args.mock_base_url}", flush=True)
    print(f"[mes_web] 监听 http://{args.host}:{args.port}/login", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
