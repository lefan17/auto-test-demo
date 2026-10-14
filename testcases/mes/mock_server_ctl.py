"""Mock MES 服务的进程管理：起、停、健康检查。

为什么要"测试自己起服务"而不是要求人先手工起：
1. 本地开发和生产流水线能跑同一套命令，不存在"我机器上是好的"；
2. 端口和数据库路径由 fixture 决定，可以并行/多次运行而不互相踩；
3. 服务起不来时给出明确报错（而不是一堆 ConnectionError 让人以为是用例 bug）。
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
API_PREFIX = "/api/v1"

# 服务统一经这层 launcher 启动：它把 uvicorn 关闭时"重发信号"的默认终止动作吸收掉，
# 让服务进程能正常返回到 atexit —— 覆盖率数据只有走到 atexit 才会落盘。
# 细节与理由见该脚本的模块注释。
SERVICE_LAUNCHER = PROJECT_ROOT / "scripts" / "service_launcher.py"

# 服务内部的"并发窗口"：在检查库存/版本与真正写库之间刻意等待的毫秒数。
# 调大一点是为了让并发用例的竞态**每次都能复现**，而不是靠运气。
# 生产代码里不存在这个等待，它只在 Mock 服务里，见 mock_server/services.py。
CONCURRENCY_WINDOW_MS = "300"


class MockServerError(RuntimeError):
    pass


def subprocess_coverage_env(data_file: Path, source: str | None = None) -> dict[str, str]:
    """构造让**被测服务子进程**自己采集覆盖率的 `COV_CORE_*` 环境变量。

    为什么是这三个变量：`pytest-cov.pth`（随 pytest-cov 装在 site-packages）在每个
    解释器启动时读 `COV_CORE_SOURCE`，命中就 `pytest_cov.embed.init()` 起一个
    `coverage.Coverage(data_suffix=True, auto_data=True)`。所以只要把它们放进子进程
    的环境，服务自己就会写分片，不需要 `coverage run` 包一层、也不需要改被测服务。

    为什么 `COV_CORE_CONFIG` 传 `os.pathsep`：这是 embed 约定的"自己去发现配置文件"
    的哨兵值（`cov_config = True`），子进程 cwd 是仓库根，于是它会读到仓库根的
    `.coveragerc`。这样 `parallel = true` 这类只能写在配置文件里的开关才有载荷点。

    什么时候需要显式调用：跑 `pytest --cov=...` 时 pytest-cov 已经把这三个变量放进
    了 `os.environ`，启动服务时自然继承，无需注入；只有在**没有 `--cov`** 的场景
    （例如 `testcases/mes/test_coverage_collection.py` 自己起服务验证机制）才需要
    显式注入，且必须把 `data_file` 指到测试自己的临时目录，断言才不会依赖外部环境。
    """
    return {
        "COV_CORE_SOURCE": str(PROJECT_ROOT) if source is None else source,
        "COV_CORE_CONFIG": os.pathsep,
        "COV_CORE_DATAFILE": str(Path(data_file).resolve()),
    }


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

    生命周期完全由测试控制：session 级起一次，session 结束无论成败都停掉，
    避免 CI 上留下僵尸进程把后续任务卡住（Job 会因为"有子进程还活着"一直不结束）。
    """

    def __init__(
        self,
        db_path: Path,
        port: int | None = None,
        concurrency_window_ms: str = CONCURRENCY_WINDOW_MS,
        reset: bool = True,
        log_path: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        self.db_path = Path(db_path)
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}{API_PREFIX}"
        self.root_url = f"http://127.0.0.1:{self.port}"
        self.concurrency_window_ms = concurrency_window_ms
        self.reset = reset
        self.log_path = Path(log_path) if log_path else None
        # 要透传给 Mock 进程的环境变量（如 MOCK_TOKEN_TTL）。
        # 为什么需要这个参数而不是靠测试进程的 os.environ：会话级服务只起一次，
        # 而 TTL 这类参数必须"每个服务实例各自不同"，共用一个全局环境变量做不到。
        self.extra_env = extra_env
        self.proc: subprocess.Popen | None = None

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 30.0) -> "MockServer":
        python = os.getenv("MOCK_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
        cmd = [
            python,
            str(SERVICE_LAUNCHER),
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
        # extra_env 排在最后：调用方要覆盖某个变量（例如把 MOCK_TOKEN_TTL 调短
        # 来制造过期令牌）时能盖住从测试进程继承来的值。TTL 是 Mock 进程启动时
        # 读进模块级常量的，服务起来之后改环境变量无效，只能在启动这一刻传进去。
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

    def stop(self, timeout: float = 10.0) -> None:
        """先礼后兵地停服务：优雅信号 → terminate → kill。

        为什么不能只留一句 terminate()：Windows 上它是 TerminateProcess，进程没有机会
        跑 atexit，而覆盖率数据（pytest-cov 的 COV_CORE_* 通道）恰恰挂在 atexit 上落盘。
        结果就是服务实打实跑了一整轮，分片文件里却一行数据都没有 —— 这就是覆盖率盲区。

        优雅信号在 Windows 上是 CTRL_BREAK_EVENT（子进程以 CREATE_NEW_PROCESS_GROUP 启动
        才收得到），在 POSIX 上是 SIGTERM；两条路 uvicorn 都会正常关闭，再由
        scripts/service_launcher.py 吸收它重发的信号，进程从 main() 返回、atexit 执行。
        优雅退出失败时退回原地 terminate 再 kill，保证不会留下卡住 CI 的僵尸进程。
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
        if getattr(self, "_log_file", None) and self._log_file is not subprocess.DEVNULL:
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

    # ---------- 辅助 ----------
    def base_url_for_tests(self) -> str:
        return self.base_url
