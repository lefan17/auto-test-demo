"""压测编排器：起服务 → 多并发梯度发压 → 落结构化结果 → 判定拐点 → 回收服务进程。

一条命令跑完，产出可复现的容量结论：

    python perf/run_load.py --users 5,10,20,50 --run-time 20s --repeat 2

为什么需要这层编排，而不是"手敲 locust 命令看屏幕"：
手敲得到的是一屏数字，没有人能回答"50 并发相比 20 并发到底退化了多少"，
因为它不留下上一次的数字。容量结论必须是**可比较的序列**：同一份代码、同一台机器、
同一组梯度，跑两次波动多少，哪个梯度开始恶化 —— 这些只有把结果落成文件才成立。

为什么用 `--csv` 而不是 `--json-file` 取数：
locust 的 `main.py:654-661` 是 `if options.json_file: ... elif ...: 打印表格`，
一旦给了 `--json-file`，**Aggregated 表就不会打印**了；而表本身是必须留档的原始证据。
`--csv` 不抑制表格，同时 `<base>_stats.csv` 里有 `50% / 95% / 99% / Requests/s / Failure Count`
这几列，是比 locust 自己的 JSON 更稳的取数口径（JSON 的百分位只有 0.95 / 0.99，
见 `locust/stats.py:120` 的 `PERCENTILES_TO_STATISTICS`，P50 得另外用 median 字段认）。

为什么 spawn-rate 等于 users：
`-r N` 让 N 个用户在 1 秒内到齐，于是 20 秒里有 19 秒是满员稳态。
若用默认的小 spawn-rate，爬坡期会占掉可观比例，量到的是"平均吞吐"而非"峰值容量"，
梯度越高被摊薄得越厉害 —— 那会把拐点算歪。

关于 `--reset`：每次起服务都重建 SQLite，保证各梯度的**起始状态完全一致**。
不用它的话，第二轮压测跑在"上一轮写场景留下的流水"上，P95 会因为表变大而漂移，
两轮之间的差异就分不清是"并发梯度造成的"还是"数据量造成的"。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCUSTFILE = Path(__file__).resolve().parent / "locustfile.py"

DEFAULT_USERS = "5,10,20,50"
DEFAULT_RUN_TIME = "20s"
DEFAULT_REPEAT = 2
DEFAULT_PORT = 8321
DEFAULT_USER_CLASSES = "MesApiUser"

# ---------------------------------------------------------------- 拐点判据
# 三条阈值都是显式数字，改结论必须先改这里，不给"凭感觉说它变慢了"留后门。
P95_DEGRADE_RATIO = 2.0  # P95 超过最低梯度 P95 的 2 倍 → 延迟拐点
P99_ABSOLUTE_MS = 1000.0  # P99 破 1 秒 → 延迟拐点
ERROR_RATE_LIMIT = 0.01  # 错误率破 1% → 稳定性拐点
SERVER_ERROR_LIMIT = 0  # 出现任何 5xx → 稳定性拐点（服务端自己的故障，不是压测脚本的锅）
RPS_SATURATION_GAIN = 0.20  # 相邻梯度 RPS 增幅 <20% → 吞吐已饱和（加人不加吞吐）

_AGGREGATED_RE = re.compile(r"^\s*Aggregated\b.*$", re.MULTILINE)


@dataclass
class ServiceHandle:
    """被测服务子进程。单独成类，是为了让"回收"有一处唯一的落点。"""

    proc: subprocess.Popen[bytes]
    port: int
    log_path: Path
    log_file: Any = field(default=None)

    def stop(self) -> dict[str, Any]:
        """优雅 → 强杀三段式回收，并返回可核验的回收证据。

        Windows 上"优雅信号"只有 `CTRL_BREAK_EVENT` 一条路：`Popen.send_signal`
        只认 `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` / `SIGTERM`，传 `signal.SIGBREAK`
        （值 21）会 `ValueError: Unsupported signal: 21`（CPython `subprocess.py`
        的 `send_signal` 实现）。首版正是拿 SIGBREAK 去发、又把 ValueError 静默吞掉，
        于是每次都走到 `terminate()`（Windows 上是 TerminateProcess，进程没有机会
        跑 shutdown），exit=1，而我却把 `graceful` 记成了 True —— 证据在撒谎。
        用 CTRL_BREAK_EVENT 并老老实实按"哪一步成功"记录。
        """
        evidence: dict[str, Any] = {"port": self.port, "graceful": False, "terminated": False, "killed": False}
        if self.proc.poll() is None:
            # 进程以 CREATE_NEW_PROCESS_GROUP 启动才收得到 CTRL_BREAK_EVENT。
            # POSIX（CI 的 ubuntu-latest）没有 CTRL_BREAK_EVENT，用 SIGTERM ——
            # uvicorn 两者都当"优雅关闭"处理，差别只在这一个符号存不存在。
            graceful_signal = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
            try:
                self.proc.send_signal(graceful_signal)
            except (ValueError, OSError, AttributeError) as exc:
                evidence["signal_error"] = f"{type(exc).__name__}: {exc}"
            try:
                self.proc.wait(timeout=10)
                evidence["graceful"] = True
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                    evidence["terminated"] = True
                except subprocess.TimeoutExpired:
                    # 兜底：连子进程树一起杀（uvicorn 理论上单进程，但 --reload 之类会 fork）
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        capture_output=True,
                        check=False,
                    )
                    self.proc.wait(timeout=10)
                    evidence["killed"] = True
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None
        evidence["exit_code"] = self.proc.returncode
        evidence["port_free_after"] = _port_is_free(self.port)
        return evidence


@dataclass
class Sample:
    """一次 (梯度, 重复) 的实测样本。"""

    users: int
    repeat: int
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    requests: int
    failures: int
    error_rate: float
    server_faults: int
    server_fault_detail: list[str]
    aggregated_line: str
    csv_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "users": self.users,
            "repeat": self.repeat,
            "rps": self.rps,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "requests": self.requests,
            "failures": self.failures,
            "error_rate": self.error_rate,
            "server_faults": self.server_faults,
            "server_fault_detail": self.server_fault_detail,
            "aggregated": self.aggregated_line,
            "csv": self.csv_path,
        }


def _port_is_free(port: int, *, attempts: int = 12, delay: float = 0.25) -> bool:
    """反复尝试 bind，能 bind 上才算真的空闲。

    两条都是实测踩出来的：

    * 不用 `connect_ex` 探测：进程刚停时端口上留着 TIME_WAIT，非阻塞 connect 返回
      WSAEWOULDBLOCK(10035) 而不是 ECONNREFUSED，于是"已释放"被误判成"仍占用"。
    * 只试一次也不够：socket 拆除是异步的，`proc.wait()` 返回不代表内核已经放开监听。
      所以给一个短暂的窗口重试，窗口内任一时刻能 bind 就算回收成功。
    """
    last = ""
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError as exc:
                last = str(exc)
        time.sleep(delay)
    print(f"[run_load] 端口 {port} 在 {attempts * delay:.1f}s 内仍不可 bind：{last}", file=sys.stderr)
    return False


def _py_libs_dir() -> Path | None:
    """定位共享依赖目录（locust 装在 `pip --target` 目录里，不在 venv 里）。

    默认取仓库同级的 `pylibs/`；可用 `LOAD_PYLIBS` 覆盖。它只影响能不能跑，
    不影响结论，所以找不到时不报错 —— 装进 venv 的环境（`pip install -r
    requirements-perf.txt`）本来就不需要它。
    """
    override = os.getenv("LOAD_PYLIBS", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None
    sibling = REPO_ROOT.parent / "pylibs"
    return sibling if sibling.is_dir() else None


def _pythonpath(existing: str) -> str:
    """拼出 locust 需要的 PYTHONPATH。

    `pip --target` 装的 pywin32 不会生效它的 `pywin32.pth`（.pth 只在 site-packages
    里被处理），所以 `win32/` 与 `win32/lib/` 必须显式加进 PYTHONPATH，否则
    gevent 导入时 `ModuleNotFoundError: No module named 'pywintypes'`。
    把这件事写进 runner，而不是要求调用者记住三条路径 —— 那正是派发者踩过的坑。
    """
    parts: list[str] = []
    libs = _py_libs_dir()
    if libs is not None:
        parts += [str(libs), str(libs / "win32"), str(libs / "win32" / "lib")]
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # URL 只指向本机被测服务
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # URL 只指向本机被测服务
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def _start_service(python: str, port: int, api_prefix: str, out_dir: Path) -> ServiceHandle:
    log_path = out_dir / f"service-{port}.log"
    log_file = log_path.open("wb")
    # 走 `scripts/service_launcher.py` 而不是直接 `-m mock_server.run`。
    # 这不是为了覆盖率好看，而是为了**回收可靠**：uvicorn 收到关闭信号后会把原 handler
    # 装回去再 `raise_signal` 重发一次，默认处置直接终止进程（实测 exit=1，atexit 不跑）；
    # launcher 先装吸收器接住重发信号，uvicorn 才能走完正常 shutdown 并返回 0。
    # 直接 `-m` 起服务时实测 `port_free_after=False`，换成 launcher 后才稳定。
    cmd = [
        python,
        str(REPO_ROOT / "scripts" / "service_launcher.py"),
        "mock_server.run",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--reset",
        # 把人工竞态窗口关掉：`mock_server/services.py:101` 的 CONCURRENCY_WINDOW_MS
        # 默认 120ms，会在"查库存"与"扣减"之间 sleep。它是为了让竞态用例必现而设的
        # 教学窗口，压测时必须关 —— 否则 P95 里量的全是这个固定 sleep，而不是服务的
        # 处理能力。关掉是**不改被测代码**的前提下唯一诚实的做法。
        "--concurrency-window-ms",
        "0",
        "--log-level",
        "warning",
    ]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(  # 参数全部来自本文件常量与 CLI
        cmd,
        cwd=str(REPO_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=flags,
    )
    handle = ServiceHandle(proc=proc, port=port, log_path=log_path, log_file=log_file)

    deadline = time.monotonic() + 60.0
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"服务启动即退出（exit={proc.returncode}），日志：{log_path}")
        code, last = _http_get(f"http://127.0.0.1:{port}{api_prefix}/readyz")
        if code == 200:
            return handle
        time.sleep(0.3)
    handle.stop()
    raise RuntimeError(f"服务 60 秒内未就绪，最后一次 /readyz 响应：{last[:300]}")


def _warm_token_pool(python: str, port: int, api_prefix: str, role: str, size: int) -> list[str]:
    """预热令牌池，避免 N 个用户在 on_start 同时跑 PBKDF2 把 CPU 打满。

    令牌是无状态 HMAC（`mock_server/security.py:103`），同一个令牌可以给所有用户用；
    这里仍然取多个，是为了不把"所有请求共用一个字符串"这种偶然优化带进结论。
    """
    code, body = _http_post_json(
        f"http://127.0.0.1:{port}{api_prefix}/auth/login",
        {"username": _role_username(python, role), "password": _role_password(python, role)},
    )
    if code != 200:
        raise RuntimeError(f"预热登录失败 status={code} body={body[:300]}")
    token = json.loads(body).get("access_token")
    if not token:
        raise RuntimeError(f"登录响应里没有 access_token：{body[:300]}")
    return [str(token)] * max(1, size)


def _role_username(python: str, role: str) -> str:
    return _common_secrets(python, "role_username", role)


def _role_password(python: str, role: str) -> str:
    return _common_secrets(python, "role_password", role)


def _common_secrets(python: str, func: str, role: str) -> str:
    """借 `common/secrets.py` 取账号口令，绝不在压测脚本里再写一份。

    用子进程而不是 `import common.secrets`：本文件要能被 `python perf/run_load.py` 直接跑，
    而 `common` 只在仓库根被加入 sys.path 时才可导入；走子进程就不依赖调用者的 cwd。
    """
    code = (
        "import sys; sys.path.insert(0, r'"
        + str(REPO_ROOT)
        + "'); from common.secrets import "
        + func
        + "; print("
        + func
        + f"({role!r}))"
    )
    out = subprocess.run([python, "-c", code], capture_output=True, check=False)  # 参数均为本文件内的常量
    if out.returncode != 0:
        raise RuntimeError(f"取账号口令失败（{func}({role!r})）：{out.stderr.decode('utf-8', 'replace')[:300]}")
    return out.stdout.decode("utf-8", "replace").strip()


def _parse_aggregated(csv_path: Path) -> dict[str, str]:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("Name") == "Aggregated":
                return {k: (v or "") for k, v in row.items() if k is not None}
    raise RuntimeError(f"{csv_path} 里没有 Aggregated 行")


# 服务端故障的**文本**特征。为什么必须单独盯它，而不是只看总错误率：
# 50 用户下总错误率只有 0.06%（4/6170），按 1% 的门限看起来"完全健康"；
# 但其中 2 条是 `500 Internal Server Error`，服务端日志对应
# `sqlite3.OperationalError: database is locked`（`mock_server/db.py:296` 的
# `BEGIN IMMEDIATE`）。这是服务**真的扛不住**的证据，却被低错误率稀释掉了 ——
# 5xx 与"压测脚本发错请求"的 4xx 混在同一个比率里，前者是容量问题、后者不是。
# 所以 5xx 单独一条判据，阈值 0：出现即拐点。
_SERVER_FAULT_PATTERNS = ("500 Server Error", "502 ", "503 ", "504 ", "ConnectionResetError")


def _parse_server_faults(failures_csv: Path) -> tuple[int, list[str]]:
    """从 locust 的 `_failures.csv` 里数服务端故障（5xx / 连接被重置）的**次数**。

    计数列是 `Occurrences`，不是行数 —— 同一错误聚合在一行里。只看行数会把
    "偶发一次"和"偶发两千次"当成同一件事。returns (次数, 每个错误的首行摘录)。
    """
    if not failures_csv.exists():
        return 0, []
    total = 0
    detail: list[str] = []
    with failures_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            error = row.get("Error") or ""
            if any(pat in error for pat in _SERVER_FAULT_PATTERNS):
                try:
                    total += int(float(row.get("Occurrences") or 0))
                except ValueError:
                    pass
                detail.append(f"{row.get('Name')}: {error[:160]}")
    return total, detail


def _run_one(
    python: str,
    users: int,
    repeat: int,
    run_time: str,
    user_classes: list[str],
    out_dir: Path,
    port: int,
    prefix: str,
    token_pool: list[str],
    role: str,
) -> Sample:
    base = out_dir / f"u{users:03d}-r{repeat}"
    cmd = [
        python,
        "-m",
        "locust",
        "--locustfile",
        str(LOCUSTFILE),
        "--headless",
        "-u",
        str(users),
        "-r",
        str(users),
        "-t",
        run_time,
        # 必须显式给 host：locust 的 User 类里没有 host 属性，不给就在启动时
        # `StopTest: You must specify the base host` 直接停表，产出 0 请求的 CSV
        # （实测踩到：表里全是 0，看起来像"服务扛住了"，其实是根本没发压）。
        "--host",
        f"http://127.0.0.1:{port}",
        "--only-summary",
        "--csv",
        str(base),
        "--csv-full-history",
        "--exit-code-on-error",
        "0",
        # 用**位置参数**选出 User 类，而不是让 locust 把两个类都实例化。
        # `locust/main.py:309-318`：不给 user_classes 时会 `list(available_user_classes.values())`
        # 全部启用并按权重平分并发名额 —— 那样 50 用户里会有一半在打登录，
        # 两个场景的数字互相污染。用 `--tags` 过滤也不行：被过滤空的类照样占名额。
        *user_classes,
    ]
    env = dict(os.environ)
    # 地雷 2 的根治：locust 启动时按 locale 读仓库根的 pyproject.toml，
    # 在中文 Windows 上会用 GBK 解码 UTF-8 的注释而崩（exit=2）。
    # 不修这个就只能靠调用者记得 `$env:PYTHONUTF8=1`，而那种约定迟早被忘。
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["LOAD_TOKEN_POOL"] = json.dumps(token_pool)
    env["LOAD_API_PREFIX"] = prefix
    env["LOAD_ROLE"] = role
    env["LOAD_THINK_TIME_MS"] = str(THINK_TIME_MS)
    env["PYTHONPATH"] = _pythonpath(env.get("PYTHONPATH", ""))

    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, check=False)  # 参数均为本文件内的常量
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    (out_dir / f"u{users:03d}-r{repeat}.locust.log").write_text(
        stdout + "\n--- stderr ---\n" + stderr, encoding="utf-8"
    )

    csv_path = base.with_name(base.name + "_stats.csv")
    if not csv_path.exists():
        raise RuntimeError(f"locust 未产出 CSV（exit={proc.returncode}）。最后 1500 字符 stderr：\n{stderr[-1500:]}")

    row = _parse_aggregated(csv_path)
    match = _AGGREGATED_RE.search(stderr) or _AGGREGATED_RE.search(stdout)
    requests = int(float(row.get("Request Count") or 0))
    failures = int(float(row.get("Failure Count") or 0))
    server_faults, server_fault_detail = _parse_server_faults(base.with_name(base.name + "_failures.csv"))

    def num(key: str) -> float:
        try:
            return float(row.get(key) or 0.0)
        except ValueError:
            return 0.0

    return Sample(
        users=users,
        repeat=repeat,
        rps=num("Requests/s"),
        p50_ms=num("50%"),
        p95_ms=num("95%"),
        p99_ms=num("99%"),
        max_ms=num("Max Response Time"),
        requests=requests,
        failures=failures,
        error_rate=(failures / requests) if requests else 0.0,
        server_faults=server_faults,
        server_fault_detail=server_fault_detail,
        aggregated_line=match.group(0).strip() if match else "(未捕获到 Aggregated 行)",
        csv_path=str(csv_path.relative_to(REPO_ROOT)) if csv_path.is_relative_to(REPO_ROOT) else str(csv_path),
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _aggregate_gradient(samples: list[Sample]) -> dict[str, Any]:
    """把一个梯度的多个重复压成一组代表值与波动幅度。"""
    p95s = [s.p95_ms for s in samples]
    rpss = [s.rps for s in samples]
    p95_med = _median(p95s)
    rep: dict[str, Any] = {
        "users": samples[0].users,
        "repeats": len(samples),
        "rps": _median(rpss),
        "p50_ms": _median([s.p50_ms for s in samples]),
        "p95_ms": p95_med,
        "p99_ms": _median([s.p99_ms for s in samples]),
        "error_rate": _median([s.error_rate for s in samples]),
        # 服务端故障取**最大**而不是中位数：这一列回答的是"这一档有没有把服务打坏"，
        # 两次重复里有一次出现 5xx 就说明这一档已经不安全。用中位数会把
        # (0, 2) 压成 1、(0, 0, 6) 压成 0 —— 前者看着像"有点问题"、后者直接消失。
        "server_faults": max((s.server_faults for s in samples), default=0),
        "requests": _median([float(s.requests) for s in samples]),
        "samples": [s.to_dict() for s in samples],
    }
    # 波动幅度只在重复 >=2 时有意义，且用极差/中位数的相对值表达 —— 绝对毫秒数
    # 在小基数上会显得很大（12ms→18ms 是"波动 50%"），相对值才说明结论稳不稳。
    if len(p95s) >= 2 and p95_med > 0:
        rep["p95_spread_ratio"] = (max(p95s) - min(p95s)) / p95_med
        rep["rps_spread_ratio"] = (max(rpss) - min(rpss)) / _median(rpss) if _median(rpss) else None
    else:
        rep["p95_spread_ratio"] = None
        rep["rps_spread_ratio"] = None
    return rep


def _detect_knee(gradients: list[dict[str, Any]]) -> dict[str, Any]:
    """按显式数字阈值判定拐点，并把判据本身一起写进结论。"""
    verdict: dict[str, Any] = {
        "criteria": {
            "p95_degrade_ratio": P95_DEGRADE_RATIO,
            "p99_absolute_ms": P99_ABSOLUTE_MS,
            "error_rate_limit": ERROR_RATE_LIMIT,
            "server_error_limit": SERVER_ERROR_LIMIT,
            "rps_saturation_gain": RPS_SATURATION_GAIN,
        },
        "latency_knee_users": None,
        "latency_knee_reason": None,
        "stability_knee_users": None,
        "stability_knee_reason": None,
        "throughput_plateau_users": None,
        "throughput_plateau_reason": None,
    }
    if not gradients:
        return verdict

    baseline = gradients[0]
    base_p95 = float(baseline["p95_ms"])
    for grad in gradients[1:]:
        users = grad["users"]
        p95 = float(grad["p95_ms"])
        p99 = float(grad["p99_ms"])
        reasons = []
        if base_p95 > 0 and p95 > base_p95 * P95_DEGRADE_RATIO:
            reasons.append(f"P95 {p95:.0f}ms > {baseline['users']}u 的 {base_p95:.0f}ms × {P95_DEGRADE_RATIO}")
        if p99 > P99_ABSOLUTE_MS:
            reasons.append(f"P99 {p99:.0f}ms > {P99_ABSOLUTE_MS:.0f}ms")
        if reasons:
            verdict["latency_knee_users"] = users
            verdict["latency_knee_reason"] = "；".join(reasons)
            break

    # 稳定性拐点单列，不与延迟拐点合并：两者可以不同档（本仓库实测就是这样 ——
    # P95 在 10u 就已经翻倍，但服务端 5xx 到 50u 才出现），合并会丢掉"哪一档
    # 开始出错"这个对容量规划更硬的信息。
    for grad in gradients:
        users = grad["users"]
        err = float(grad["error_rate"])
        faults = int(grad.get("server_faults") or 0)
        reasons = []
        if faults > SERVER_ERROR_LIMIT:
            reasons.append(f"服务端故障 {faults} 次（5xx / 连接被重置），阈值 {SERVER_ERROR_LIMIT}")
        if err > ERROR_RATE_LIMIT:
            reasons.append(f"总错误率 {err * 100:.2f}% > {ERROR_RATE_LIMIT * 100:.0f}%")
        if reasons:
            verdict["stability_knee_users"] = users
            verdict["stability_knee_reason"] = "；".join(reasons)
            break

    for prev, grad in zip(gradients, gradients[1:], strict=False):
        prev_rps = float(prev["rps"])
        gain = (float(grad["rps"]) - prev_rps) / prev_rps if prev_rps else 0.0
        if gain < RPS_SATURATION_GAIN:
            verdict["throughput_plateau_users"] = grad["users"]
            verdict["throughput_plateau_reason"] = (
                f"{grad['users']}u 的 RPS 相对 {prev['users']}u 只涨了 {gain * 100:.1f}%"
                f"（{prev_rps:.1f} → {float(grad['rps']):.1f}），低于 {RPS_SATURATION_GAIN * 100:.0f}% 判据"
            )
            break
    return verdict


THINK_TIME_MS = int(os.getenv("LOAD_THINK_TIME_MS", "0"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_load", description="MES Mock 服务容量压测")
    parser.add_argument("--python", default=sys.executable, help="解释器路径（默认当前解释器）")
    parser.add_argument("--users", default=DEFAULT_USERS, help="逗号分隔的并发梯度")
    parser.add_argument("--run-time", default=DEFAULT_RUN_TIME)
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="每个梯度重复次数（重复性证据）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--api-prefix", default=os.getenv("LOAD_API_PREFIX", "/api/v1"))
    parser.add_argument("--role", default=os.getenv("LOAD_ROLE", "ADMIN"))
    parser.add_argument("--user-classes", default=DEFAULT_USER_CLASSES, help="空格分隔的 locust User 类名")
    parser.add_argument("--out", default="reports/perf")
    parser.add_argument("--label", default="", help="结果目录后缀，便于区分不同批次")
    parser.add_argument("--list-only", action="store_true", help="只打印将要执行的批次计划后退出，不发压")
    args = parser.parse_args(argv)

    gradients_wanted = [int(x) for x in args.users.split(",") if x.strip()]
    user_classes = [t for t in args.user_classes.split() if t]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = f"-{args.label}" if args.label else ""
    out_dir = (REPO_ROOT / args.out / f"{stamp}{label}").resolve()

    if args.list_only:
        print(f"导出目录   : {out_dir}")
        print(f"解释器     : {args.python}")
        print(f"locust 类  : {', '.join(user_classes)}")
        print(f"梯度 x 重复: {gradients_wanted} x {args.repeat}")
        print(f"单轮时长   : {args.run_time}   端口: {args.port}   角色: {args.role}")
        print(f"思考时间   : {THINK_TIME_MS}ms（0 = 量服务端吞吐上限）")
        print(f"预计请求数 : 约 {int(sum(gradients_wanted) * args.repeat * 20)} 条量级")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    if not _port_is_free(args.port):
        print(f"[run_load] 端口 {args.port} 已被占用，换 --port 或先清掉占用进程", file=sys.stderr)
        return 2

    if not LOCUSTFILE.exists():
        print(f"[run_load] 找不到 {LOCUSTFILE}", file=sys.stderr)
        return 2

    print(f"[run_load] 产出目录 {out_dir}", flush=True)
    service = _start_service(args.python, args.port, args.api_prefix, out_dir)
    print(f"[run_load] 服务已就绪 pid={service.proc.pid} port={args.port}", flush=True)
    stop_evidence: dict[str, Any] = {}
    samples: list[Sample] = []
    try:
        pool = _warm_token_pool(
            args.python, args.port, args.api_prefix, args.role, size=max(1, min(8, max(gradients_wanted)))
        )
        print(f"[run_load] 令牌池 {len(pool)} 个（{args.role}）", flush=True)
        for users in gradients_wanted:
            for repeat in range(1, args.repeat + 1):
                sample = _run_one(
                    args.python,
                    users,
                    repeat,
                    args.run_time,
                    user_classes,
                    out_dir,
                    args.port,
                    args.api_prefix,
                    pool,
                    args.role,
                )
                samples.append(sample)
                print(
                    f"[run_load] {users}u r{repeat}: RPS={sample.rps:.1f} "
                    f"P50={sample.p50_ms:.0f} P95={sample.p95_ms:.0f} P99={sample.p99_ms:.0f} "
                    f"错误率={sample.error_rate * 100:.2f}% ({sample.requests} req)",
                    flush=True,
                )
    finally:
        stop_evidence = service.stop()
        print(
            f"[run_load] 服务回收：优雅关闭={stop_evidence['graceful']} "
            f"强制终止={stop_evidence['terminated']} 硬杀={stop_evidence['killed']} "
            f"exit={stop_evidence['exit_code']} 端口已释放={stop_evidence['port_free_after']}",
            flush=True,
        )

    jsonl_path = out_dir / "samples.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")

    by_users: dict[int, list[Sample]] = {}
    for sample in samples:
        by_users.setdefault(sample.users, []).append(sample)
    gradients = [_aggregate_gradient(by_users[u]) for u in sorted(by_users)]
    summary = {
        "generated_at": stamp,
        "repo_commit": subprocess.run(  # 参数均为本文件内的常量
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=False
        ).stdout.strip(),
        "config": {
            "users": gradients_wanted,
            "repeat": args.repeat,
            "run_time": args.run_time,
            "user_classes": user_classes,
            "role": args.role,
            "api_prefix": args.api_prefix,
            "think_time_ms": THINK_TIME_MS,
            "spawn_rate": "= users（1 秒到齐，目的是量满员稳态而非爬坡均值）",
            "service_reset_per_run": True,
        },
        "environment": {
            "host": os.environ.get("COMPUTERNAME", "unknown"),
            "os": os.environ.get("OS", "unknown"),
            "python": args.python,
            "cpu_count": os.cpu_count(),
            "note": "压测机与被测服务同机（127.0.0.1）",
        },
        "gradients": gradients,
        "knee": _detect_knee(gradients),
        "service_shutdown": stop_evidence,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[run_load] 明细 {jsonl_path}", flush=True)
    print(f"[run_load] 汇总 {summary_path}", flush=True)
    print(json.dumps(summary["knee"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
