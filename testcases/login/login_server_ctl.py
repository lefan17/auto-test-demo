"""被测系统（login_app）的进程管理：起、停、健康检查。

为什么不复用 mock_server_ctl.MockServer：
    MES 那套的生命周期里绑了 **SQLite 数据库**（`--db`、`--reset`、
    探针要核对"连的是哪个库"）。登录服务是无状态的，没有库可核对，
    硬套会变成一堆 `if db is not None` 的分支。
    两个类共用同一个模式（子进程 + 系统分配端口 + 探针 + 必停），
    但各自的启动参数不同，分开写更清楚。

为什么"测试自己起服务"：与 MES 同样的三条理由 ——
    1. 本地和 CI 跑同一条命令，不存在"我机器上是好的"；
    2. 端口由系统分配，可以并行/重复运行而不互相踩；
    3. 服务起不来时给出明确报错，而不是一堆 ConnectionError 让人以为是用例写错了。
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


class LoginServerError(RuntimeError):
    pass


def free_port() -> int:
    """让操作系统分配一个空闲端口。

    写死端口的问题：上次跑崩留下的进程还在占着，新的一次直接起不来，
    报错是 "address already in use"，容易误判成代码坏了。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LoginServer:
    """以子进程方式起停被测登录服务。

    生命周期完全由测试控制：session 级起一次，session 结束无论成败都 kill。
    """

    def __init__(self, port: int | None = None, log_path: Path | None = None):
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = Path(log_path) if log_path else None
        self.proc: subprocess.Popen | None = None
        self._log_file = None

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 30.0) -> "LoginServer":
        python = os.getenv("LOGIN_PYTHON") or (
            str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable
        )
        cmd = [
            python,
            "-m",
            "login_app.run",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            "warning",
        ]

        # 日志落文件而不是接管道：子进程写满管道缓冲区会卡死
        # （Windows 上 pip/pytest 的输出管道问题在本机反复出现过）。
        self._log_file = (
            open(self.log_path, "w", encoding="utf-8") if self.log_path else subprocess.DEVNULL
        )
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT_ROOT)}
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
            # 进程组独立：父进程被 Ctrl+C 打断时不会连累子进程卡住
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._wait_healthy(timeout)
        return self

    def _wait_healthy(self, timeout: float) -> None:
        """轮询探针直到服务就绪。

        探针要核对**版本号**，不只判断"端口通了"：
        端口通只说明有东西在监听，可能是上一次没死干净、或被别的程序占了。
        核对版本能确保连上的确实是本次启动的这个服务。
        """
        from login_app.app import APP_VERSION

        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise LoginServerError(
                    f"登录服务进程已退出（returncode={self.proc.returncode}）。"
                    f"启动日志: {self._read_log_tail()}"
                )
            try:
                with urllib.request.urlopen(f"{self.base_url}/api/health", timeout=1.0) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if body.get("status") != "ok":
                    raise LoginServerError(f"探针返回异常: {body}")
                if body.get("version") != APP_VERSION:
                    raise LoginServerError(
                        f"连到的服务版本不对：期望 {APP_VERSION}，实际 {body.get('version')}。"
                        "多半是上一次运行的残留进程还占着这个端口。"
                    )
                return
            except (urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
                last_err = exc
                time.sleep(0.2)
        raise LoginServerError(f"登录服务 {timeout}s 内未就绪: {last_err}\n{self._read_log_tail()}")

    def _read_log_tail(self, lines: int = 30) -> str:
        if not self.log_path:
            return "(未开启日志文件)"
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
        if self._log_file is not None and self._log_file is not subprocess.DEVNULL:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
