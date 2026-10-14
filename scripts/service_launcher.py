"""以「进程能正常返回」的方式启动被测服务子进程。

为什么需要这一层（Windows 上的覆盖率盲区的直接原因）：

* uvicorn 收到 SIGINT/SIGTERM/SIGBREAK 后，`capture_signals()` 的 finally 会把
  原来的 handler 装回去，然后 `signal.raise_signal(原信号)` **把信号重发给自己**，
  让"默认处置"结束进程。默认处置不跑 `atexit`。
* 覆盖率数据（无论 pytest-cov 的 `COV_CORE_*` 通道还是 coverage 自己的
  `COVERAGE_PROCESS_START` 通道）都挂在 `atexit` 上落盘。于是服务跑了一整轮，
  分片文件却只有启动时写下的空壳 —— 这就是 `mock_server/*` 覆盖率恒 0% 的原因。
* 在本进程里先装一个"吸收重发信号"的 handler，uvicorn 重发时被它接住（不退出），
  `serve()` 正常返回 → `main()` 返回 → `sys.exit(0)` → `atexit` 执行 → 分片落盘。

为什么写成独立脚本而不是改 `mock_server/run.py`：
覆盖率不该要求被测对象改代码。这个脚本对被测服务完全透明，只是把它当成模块跑。
"""

from __future__ import annotations

import runpy
import signal
import sys

# 被吸收的信号：uvicorn 关闭后重发的那个会落在这里，只记不看。
_ABSORBED: list[int] = []


def _absorb(signum: int, frame: object) -> None:
    _ABSORBED.append(signum)


def install_signal_absorbers() -> list[int]:
    """把 SIGINT/SIGTERM/SIGBREAK 换成"只记录、不终止"的 handler，返回装上的信号号。"""
    installed = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:  # SIGBREAK 只在 Windows 存在
            continue
        try:
            signal.signal(sig, _absorb)
        except (ValueError, OSError):
            # 非主线程、或平台不支持该信号时跳过即可，不影响启动
            continue
        installed.append(int(sig))
    return installed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法: python scripts/service_launcher.py <模块名> [模块参数...]", file=sys.stderr)
        return 2
    module, rest = args[0], args[1:]
    install_signal_absorbers()
    # 被测模块用 `if __name__ == "__main__": sys.exit(main())` 收尾，所以要让它看到
    # 自己的命令行：argv[0] 是模块名，其余原样透传。
    sys.argv = [module, *rest]
    try:
        runpy.run_module(module, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
