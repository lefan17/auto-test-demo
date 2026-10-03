"""Mock MES 服务的进程管理：起、停、健康检查。

为什么要"测试自己起服务"而不是要求人先手工起：
1. 本地开发和生产流水线能跑同一套命令，不存在"我机器上是好的"；
2. 端口和数据库路径由 fixture 决定，可以并行/多次运行而不互相踩；
3. 服务起不来时给出明确报错（而不是一堆 ConnectionError 让人以为是用例 bug）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
API_PREFIX = "/api/v1"

# 服务内部的"并发窗口"：在检查库存/版本与真正写库之间刻意等待的毫秒数。
# 调大一点是为了让并发用例的竞态**每次都能复现**，而不是靠运气。
# 生产代码里不存在这个等待，它只在 Mock 服务里，见 mock_server/services.py。
CONCURRENCY_WINDOW_MS = "300"


class MockServerError(RuntimeError):
    pass


def free_port() -> int:
    """让操作系统分配一个空闲端口。

    写死 8000 的问题：上次跑崩留下的 uvicorn 还在占端口，新的一次直接起不来，
    报错还是 "address already in use"，容易误判成代码坏了。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockServer:
    """以子进程方式起停 Mock MES 服务。

    生命周期完全由测试控制：session 级起一次，session 结束无论成败都 kill，
    避免 CI 上留下僵尸进程把后续任务卡住（Job 会因为"有子进程还活着"一直不结束）。
    """

    def __init__(
        self,
        db_path: Path,
        port: int | None = None,
        concurrency_window_ms: str = CONCURRENCY_WINDOW_MS,
        reset: bool = True,
        log_path: Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}{API_PREFIX}"
        self.root_url = f"http://127.0.0.1:{self.port}"
        self.concurrency_window_ms = concurrency_window_ms
        self.reset = reset
        self.log_path = Path(log_path) if log_path else None
        self.proc: subprocess.Popen | None = None

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 30.0) -> "MockServer":
        python = os.getenv("MOCK_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
        cmd = [
            python,
            "-m",
            "mock_server.run",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--db",
            str(self.db_path),
            "--concurrency-window-ms",
            str(self.concurrency_window_ms),
            "--log-level",
            "warning",
        ]
        if self.reset:
            cmd.append("--reset")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 用 PIPE 之外的方式收集日志：不能接管道读，否则子进程写满缓冲区会卡死
        # （Windows 上 pip/pytest 的输出管道问题在本机反复出现过）。
        self._log_file = open(self.log_path, "w", encoding="utf-8") if self.log_path else subprocess.DEVNULL
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT_ROOT)}
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
            # 关键：进程组独立，父进程被 Ctrl+C 打断时不会连累子进程卡住
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._wait_healthy(timeout)
        return self

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise MockServerError(
                    f"Mock 服务进程已退出（returncode={self.proc.returncode}）。启动日志: {self._read_log_tail()}"
                )
            try:
                with urllib.request.urlopen(f"{self.root_url}{API_PREFIX}/healthz", timeout=1.0) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                # 健康检查必须核对"连的是哪个库"。曾经出现过：服务起来了、探针也
                # 200，但读请求落在默认库上（写库却写对了），这样测试会在错误的
                # 数据上跑还全绿。把库路径写进探针并在启动时核对，是最省事的防呆。
                actual = Path(body.get("database", "")).resolve()
                if actual != self.db_path.resolve():
                    raise MockServerError(f"Mock 服务连的库不对：期望 {self.db_path.resolve()}，实际 {actual}")
                return
            except (urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
                last_err = exc
                time.sleep(0.2)
        raise MockServerError(f"Mock 服务 {timeout}s 内未就绪: {last_err}\n{self._read_log_tail()}")

    def _read_log_tail(self, lines: int = 30) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return "(无日志)"
        return "\n".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if getattr(self, "_log_file", None) and self._log_file is not subprocess.DEVNULL:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 辅助 ----------
    def base_url_for_tests(self) -> str:
        return self.base_url
