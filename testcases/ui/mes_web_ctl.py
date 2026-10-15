"""Demo Web（`mes_web`）的进程管理：起、停、健康检查。

为什么测试自己起服务而不是要求人先手工起：本地和 CI 能跑同一条命令，端口和日志路径由
fixture 决定，可以并行/重复运行而不互相踩；服务起不来时给明确报错，而不是一堆
`ConnectionError` 让人误以为是用例 bug。

四个决定直接照抄 `testcases/mes/mock_server_ctl.py` 已经验证过的做法（契约 §6.1 要求不重新发明）：

1. **解释器解析**：`MES_WEB_PYTHON` → 仓库 `.venv`（本机不存在，CI 上是 Linux 同样不存在）
   → `sys.executable` 回落。回落正是想要的行为。
2. **端口由 OS 分配**：`free_port()` 绑 0 取端口，不写死——上次跑崩留下的进程占着端口会让
   新一次直接起不来，报错还是 `address already in use`，容易误判成代码坏了。
3. **日志写文件而不是 `PIPE`**：子进程写满管道缓冲区会卡死（Windows 上反复出现过）。
4. **`_wait_healthy` 核对 `/healthz` 回显的 `mock_base_url`**：防「探针 200 但转发到了错后端，
   跑在错数据上还全绿」。

比 `mock_server_ctl` 多出来一条检查：**探活时还要求后端本身可达**。原因是 Demo Web 的
`/healthz` 是纯回显（契约 §3 冻结），它不回显就等于没转发过任何请求——只看它的话，
把 `--mock-base-url` 指向一个根本没有服务的端口也会「健康」。而转发型服务的全部价值就在那个后端上，
所以起服务时就把后端探一遍，让错配在 fixture 阶段炸掉，而不是等用例里出现一张空列表再查半天。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 裸名导入：`testcases/ui/` 有 `__init__.py`，但 `testcases/mes/` 没有，且根 `conftest.py`
# 把 `MES_DIR` 插进了 `sys.path`，所以 `mock_server_ctl` 是**顶层模块**，裸名可行
# （契约 §6.2 实测过）。`free_port` 直接重新导出，不重复实现。
from mock_server_ctl import free_port as free_port

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
LOG_DIR = PROJECT_ROOT / ".tmp" / "mes_web"
# 只用于「后端可达性」探测：`--mock-base-url` 传的是根 URL，前缀由 mes_web 自己补
# （契约 §3）。这里探的是 mock_server 自己的健康端点，它不需要令牌。
API_PREFIX = "/api/v1"


class MesWebError(RuntimeError):
    pass


class MesWebServer:
    """以子进程方式起停 Demo Web。

    生命周期完全由测试控制：session 级起一次，session 结束无论成败都 kill，
    避免 CI 上留下僵尸进程把后续 Job 卡住。
    """

    def __init__(
        self,
        mock_base_url: str,
        port: int | None = None,
        log_path: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        # 传进来的必须是 mock_server 的**根** URL（`MockServer.root_url`，不含 `/api/v1`）。
        # 这里只做 rstrip，不做补前缀/去前缀——错配就是要让 `/healthz` 的核对把它抓出来，
        # 而不是被一层「友好修正」掩盖掉。末尾斜杠规范化仍要做：`http://h:1/` 与
        # `http://h:1` 是同一个后端，不该被判成不一致。
        self.mock_base_url = mock_base_url.rstrip("/")
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = Path(log_path) if log_path else None
        self.extra_env = extra_env
        self.proc: subprocess.Popen | None = None

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 30.0) -> "MesWebServer":
        python = os.getenv("MES_WEB_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)
        cmd = [
            python,
            "-m",
            "mes_web.run",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--mock-base-url",
            self.mock_base_url,
            "--log-level",
            "warning",
        ]

        if self.log_path is None:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.log_path = LOG_DIR / f"mes_web-{self.port}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.log_path, "w", encoding="utf-8")
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
        """等到 Demo Web 就绪**且**它的后端可达。

        两件事分开报错：探针连不上（`last_err`）与探针 200 但后端不可达（`backend_err`）
        是两类完全不同的故障，混成一句「N 秒内未就绪」会把人引到错的方向——
        实测过一次：`--mock-base-url` 指到空端口时，Demo Web 本身起得好好的，
        但报出来的却是「Demo Web 6.0s 内未就绪: <urlopen error timed out>」。
        """
        deadline = time.time() + timeout
        last_err: Exception | None = None
        backend_err: Exception | None = None
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise MesWebError(
                    f"Demo Web 进程已退出（returncode={self.proc.returncode}）。启动日志: {self._read_log_tail()}"
                )
            try:
                with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=1.0) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except (
                urllib.error.URLError,
                ConnectionError,
                OSError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                last_err = exc
                time.sleep(0.2)
                continue

            # 探针成功就把启动早期的失败清掉：否则服务刚起时那一两次
            # `ConnectionRefused` 会一直粘在 last_err 上，把「后端不可达」误报成「Demo Web 未就绪」。
            last_err = None

            # 探针 200 还不够：曾经出现过的错法是 Demo Web 起来了、/healthz 也 200，
            # 但它转发到的是**另一个** mock_server（例如把带 /api/v1 的 base_url 当根 URL
            # 传进来，或端口传错）。那样转发全 404，而用例只看到「列表是空的」，
            # 会当成业务问题查半天。所以这里核对它自己声明连的是哪个后端。
            actual = str(body.get("mock_base_url", ""))
            if actual != self.mock_base_url:
                raise MesWebError(f"Demo Web 连的后端不对：期望 {self.mock_base_url}，实际 {actual or '(空)'}")

            # 后端可达性：见模块 docstring 第 4 条。Demo Web 的 /healthz 是纯回显，
            # 它 200 不代表被转发的那一端活着——把 --mock-base-url 指到一个空端口同样会 200。
            # 探一个必然被转发的端点，把「后端不存在」在 fixture 阶段就变成明确报错。
            try:
                with urllib.request.urlopen(f"{self.mock_base_url}{API_PREFIX}/healthz", timeout=1.0) as probe:
                    json.loads(probe.read().decode("utf-8"))
            except (
                urllib.error.URLError,
                ConnectionError,
                OSError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                backend_err = exc
                time.sleep(0.2)
                continue
            return

        if backend_err is not None and last_err is None:
            raise MesWebError(
                f"Demo Web 已在 {self.base_url} 就绪，但它的后端 {self.mock_base_url} 在 {timeout}s 内不可达: "
                f"{backend_err}\nDemo Web 启动日志: {self._read_log_tail()}"
            )
        raise MesWebError(
            f"Demo Web {timeout}s 内未就绪（{self.base_url}/healthz）: {last_err}\n{self._read_log_tail()}"
        )

    def _read_log_tail(self, lines: int = 30) -> str:
        if self.log_path is None:
            return "(无日志)"
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
        log_file = getattr(self, "_log_file", None)
        if log_file is not None and log_file is not subprocess.DEVNULL:
            try:
                log_file.close()
            except Exception:  # noqa: BLE001
                pass
