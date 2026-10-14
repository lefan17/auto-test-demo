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
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

# 服务统一经这层 launcher 启动：它把 uvicorn 关闭时"重发信号"的默认终止动作吸收掉，
# 让服务进程能正常返回到 atexit —— 覆盖率数据只有走到 atexit 才会落盘。
# 细节与理由见该脚本的模块注释。
SERVICE_LAUNCHER = PROJECT_ROOT / "scripts" / "service_launcher.py"


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

    def __init__(self, port: int | None = None, log_path: Path | None = None, extra_env: dict[str, str] | None = None):
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = Path(log_path) if log_path else None
        # 要透传给登录服务进程的环境变量（例如注入 COV_CORE_* 让它自己采覆盖率）。
        # 与 MockServer 的同名参数一致：让"每个服务实例各自不同"的变量不必挤在全局环境里。
        self.extra_env = extra_env
        self.proc: subprocess.Popen | None = None
        self._log_file = None

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 30.0) -> "LoginServer":
        python = os.getenv("LOGIN_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
        cmd = [
            python,
            str(SERVICE_LAUNCHER),
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
        self._log_file = open(self.log_path, "w", encoding="utf-8") if self.log_path else subprocess.DEVNULL
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(PROJECT_ROOT),
            **(self.extra_env or {}),
        }
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
                    f"登录服务进程已退出（returncode={self.proc.returncode}）。启动日志: {self._read_log_tail()}"
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

    def stop(self, timeout: float = 10.0) -> None:
        """先礼后兵地停服务：优雅信号 → terminate → kill。

        为什么不能只留一句 terminate()：Windows 上它是 TerminateProcess，进程没有机会
        跑 atexit，而覆盖率数据挂在 atexit 上落盘 —— 服务跑了整轮，覆盖率却一行没有。
        优雅信号在 Windows 上是 CTRL_BREAK_EVENT（子进程以 CREATE_NEW_PROCESS_GROUP 启动
        才收得到），在 POSIX 上是 SIGTERM；uvicorn 收到后正常关闭，再由
        scripts/service_launcher.py 吸收它重发的信号，进程从 main() 返回、atexit 执行。
        优雅退出失败时退回 terminate 再 kill，保证不留下卡住 CI 的僵尸进程。
        """
        if self.proc and self.proc.poll() is None:
            self._signal_graceful()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=timeout)
        if self._log_file is not None and self._log_file is not subprocess.DEVNULL:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass

    def _signal_graceful(self) -> None:
        """发一个"请正常关闭"的信号。"""
        if self.proc is None:
            return
        # CTRL_BREAK_EVENT 只在 Windows 存在（用 getattr 取，避免在其它平台被静态检查报未定义）。
        sig = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM) if os.name == "nt" else signal.SIGTERM
        try:
            self.proc.send_signal(sig)
        except (OSError, ValueError):
            # 进程刚好已经退出时忽略：下面的 wait() 会拿到它的 returncode。
            pass
