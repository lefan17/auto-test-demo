# 冻结接口（调度者冻结，子代理不得修改本文件）

两个 B 子代理并行工作，彼此看不见对方的代码。凡跨目录调用的签名都写在这里，
**按本文件实现，不要自行改名或改签名**。发现设计问题就在回报里提出，不要擅自改契约。

## 1. 数据模型

`replay/model.py` 已存在，是四方共享的唯一定义。禁止修改其中的名字、字段、顺序。
只允许**追加**带默认值的字段（frozen dataclass 默认值必须放末尾）。

## 2. `recorder/store.py` —— 由 recorder 代理实现，replayer 侧只调用不修改

```python
@dataclass(frozen=True)
class BaselineInfo:
    name: str
    baseline_id: str
    created_at: datetime          # tz-aware UTC
    exchange_count: int

class Repository:
    def __init__(self, db_path: str | Path) -> None: ...
    def migrate(self) -> None: ...
        """建表，幂等，可重复调用。"""
    def save_baseline(self, name: str, exchanges: Iterable[RecordedExchange]) -> str: ...
        """批量写入并返回 baseline_id（uuid4 hex 前 16 位）。同名覆盖旧的。"""
    def get_baseline_id(self, name: str) -> str | None: ...
    def list_baselines(self) -> list[BaselineInfo]: ...
        """按 created_at 降序。"""
    def load_baseline(self, baseline_id: str) -> list[RecordedExchange]: ...
        """未知 id 抛 KeyError。返回顺序 = 写入顺序。"""
    def save_report(self, report: ReplayReport) -> None: ...
    def latest_report(self, baseline_id: str | None = None) -> ReplayReport | None: ...
        """无记录返回 None。"""
```

约束：
- SQLite 单文件，schema 版本号写进 `PRAGMA user_version`，`migrate()` 幂等。
- `RecordedExchange` 的 `body_json` 以 JSON 文本存，`body`（bytes）以 BLOB 存；二者都可为空。
- `recorded_at` 存 ISO 8601 带时区字符串。
- 连接用 `sqlite3`，`check_same_thread=False`，写入用单事务。
- 不得引入 SQLAlchemy 或任何新依赖。

## 3. `replay/player.py` —— 由 replayer 代理实现

```python
class ExchangePlayer:
    def __init__(
        self,
        base_url: str,                 # 回放目标，如 http://127.0.0.1:18000
        *,
        concurrency: int = 8,
        qps: float | None = None,
        timeout: float = 15.0,
        sources: "ReplaySources | None" = None,
    ) -> None: ...
    async def replay(self, exchanges: Sequence[RecordedExchange]) -> tuple[ReplayOutcome, ...]: ...
    def run(self, exchanges: Sequence[RecordedExchange]) -> tuple[ReplayOutcome, ...]: ...
        """同步包装（内部 asyncio.run），供 CLI 与测试调用。"""
```

- 回放只覆盖请求的 `path`/`query`/`body`/headers 白名单；**不得**把录制时的 `Host`、
  `Content-Length`、`Authorization` 原样透传（token 由调用方通过 `headers_override` 注入）。
- 写方法（POST/PUT/PATCH/DELETE）默认走**影子模式**：`ReplaySources.shadow_headers`
  非空时附加影子路由头，空时仍发请求但调用方需自行保证目标环境可写。
- QPS 用信号量 + 最小间隔实现，不得引入 `pyrate-limiter` 之类新依赖。

## 4. 目录归属（硬边界，越界即返工）

| 路径 | 归属 |
|---|---|
| `traffic-replay/replay/model.py` | **冻结**，谁都不改 |
| `traffic-replay/conftest.py`、`traffic-replay/pytest.ini` | **冻结**，谁都不改 |
| `traffic-replay/FROZEN_INTERFACES.md` | **冻结**，谁都不改 |
| `traffic-replay/recorder/**` | recorder 代理独占 |
| `traffic-replay/fixtures/**` | recorder 代理独占 |
| `traffic-replay/tests/test_recorder_*.py` | recorder 代理独占 |
| `traffic-replay/replay/player.py`、`replay/differ.py` | replayer 代理独占 |
| `traffic-replay/analyze/**` | replayer 代理独占 |
| `traffic-replay/tests/test_replay_*.py`、`tests/test_differ_*.py` | replayer 代理独占 |

## 5. 运行环境（实测，别再试探）

- 解释器：`C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`（Python 3.12.10）
- `httpx` 需 `$env:PYTHONPATH='C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'` 才可导入。
- **mitmproxy 未安装**：`ProxyTap` 必须是懒加载接入点（函数内 import），
  模块顶层 import mitmproxy 会让本地链路直接崩。测试里用 monkeypatch 伪造。
- 沙箱只允许写 `C:\Users\lefan\DSH_project\接口自动化_3` 之下；写 `C:\Users\lefan\DSH_project\traffic-replay` 会被拒。
- PowerShell 输出中文需 `chcp 65001` + `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
