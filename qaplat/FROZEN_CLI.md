# 平台层冻结契约（调度者冻结，子代理不得改签名）

## 为什么包名是 `qaplat/` 而不是 `platform/`

选题方案里写的是 `platform/`，**这里必须改掉**：`work/auto-test-demo/conftest.py:29-31`
把仓库根 `ROOT_DIR` 插入 `sys.path[0]`。若仓库根存在 `platform/` 目录，它会遮蔽
**标准库 `platform` 模块**——而 `pytest` 自身的 `_pytest/` 在启动时就 `import platform`，
结果是整套测试直接崩在收集阶段。`qaplat/` 无同名冲突。

## 一、CLI 契约（`python -m qaplat.cli <cmd>`）

所有子命令都必须支持 `--help`，退出码语义固定：

| 命令 | 作用 | 退出码 |
|---|---|---|
| `qaplat run --layer <smoke\|pr\|nightly\|release> [-- pytest 参数...]` | 按层拼 `-m` 表达式并调用 pytest | pytest 退出码原样透传 |
| `qaplat gate --layer <层级>` | 跑该层 **并落库** 本轮结果，再与阈值比对 | 0 达标 / 1 未达标 / 2 CLI 用法错误 |
| `qaplat registry build [--json PATH]` | 生成「用例 ↔ operationId 覆盖矩阵」 | 0 |
| `qaplat registry gaps` | 只打印未被任何用例覆盖的 operationId | 0 |
| `qaplat metrics show [--last N]` | 打印最近 N 轮（默认 10）通过率/耗时/flaky | 0 |
| `qaplat metrics flaky [--last N]` | 打印跨轮次不稳定用例清单 | 0 |
| `qaplat report [--out DIR]` | 生成静态 HTML 报表（含契约漂移页） | 0 |
| `qaplat drift record --commit <sha>` | 跑契约漂移检查并把结果落库 | 0 无漂移 / 1 有漂移 |

`--db PATH` 是全局选项，默认 `qaplat/.cache/qaplat.db`（该目录须进 `.gitignore`）。

## 二、四层门禁的 `-m` 表达式（固定，不得自创 marker）

现有 `pytest.ini` 已注册 marker：`smoke / regression / api / ui / mes / login / contract`。
本层新增且必须注册：`platform`。各层表达式：

| 层 | `-m` 表达式 | 目标时长 |
|---|---|---|
| smoke | `smoke` | < 1 min |
| pr | `smoke or api or platform` | 3–5 min |
| nightly | `not ui` | 全量非 UI |
| release | `` （空，全量，含 ui） | 含 UI |

## 三、`qaplat/gate.py` 必须导出的东西

```python
class Layer(StrEnum):
    SMOKE = "smoke"
    PR = "pr"
    NIGHTLY = "nightly"
    RELEASE = "release"


@dataclass(frozen=True)
class LayerSpec:
    layer: Layer
    marker_expr: str
    timeout_seconds: int
    min_pass_rate: float
    max_duration_seconds: int


LAYERS: dict[Layer, LayerSpec]


def build_pytest_args(spec: LayerSpec, extra: Sequence[str] = ()) -> list[str]: ...


@dataclass(frozen=True)
class GateResult:
    layer: Layer
    run_id: str
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    duration_seconds: float
    pass_rate: float
    within_budget: bool
    violations: tuple[str, ...]


def evaluate(spec: LayerSpec, run: "RunRecord") -> GateResult: ...
```

## 四、`qaplat/metrics.py` 必须导出的东西

```python
@dataclass(frozen=True)
class RunRecord:
    run_id: str
    layer: str
    commit_sha: str | None
    started_at: datetime          # tz-aware UTC
    duration_seconds: float
    total: int; passed: int; failed: int; errors: int; skipped: int; xfailed: int
    pass_rate: float
    outcome: str                  # "passed" | "failed" | "error"

class MetricsStore:
    def __init__(self, db_path: str | Path) -> None: ...
    def migrate(self) -> None: ...                       # 幂等
    def record_run(self, run: RunRecord, cases: Sequence["CaseOutcome"]) -> None: ...
    def record_drift(self, commit_sha: str, drifted: bool, changed: Sequence[str]) -> None: ...
    def recent_runs(self, limit: int = 10) -> list[RunRecord]: ...
    def flaky_cases(self, last_n_runs: int = 10) -> list[tuple[str, int, int]]: ...
        """返回 (case_id, 通过轮次, 出现轮次)，只含「有通过也有失败」的用例。"""
    def contract_drift_history(self, limit: int = 20) -> list["DriftRecord"]: ...

@dataclass(frozen=True)
class CaseOutcome:
    run_id: str; case_id: str; outcome: str; duration_seconds: float

@dataclass(frozen=True)
class DriftRecord:
    commit_sha: str; checked_at: datetime; drifted: bool; changed: tuple[str, ...]
```

- 纯 `sqlite3`，`check_same_thread=False`，schema 版本写进 `PRAGMA user_version`。
- 不得引入 SQLAlchemy 或任何新依赖（依赖清单已冻结在 `requirements.txt`）。
- 失败归因字段：`CaseOutcome.outcome ∈ {passed, failed, error, skipped, xfailed}`；
  归因分类（环境/数据/被测缺陷/真实回归）放 `qaplat/classify.py`，签名自定但必须在
  `qaplat report` 里出现。

## 五、`qaplat/registry.py` 必须导出的东西

```python
@dataclass(frozen=True)
class OperationCoverage:
    operation_id: str
    method: str
    path: str
    covered_by: tuple[str, ...]      # 用例 nodeid，稳定排序

def load_operations(spec_path: str | Path) -> list[tuple[str, str, str]]: ...
    """返回 (operation_id, method, path)。operation_id 缺失时用 f"{method} {path}" 兜底。"""

def build_coverage(
    cases: Sequence["CaseInfo"], operations: Sequence[tuple[str, str, str]]
) -> list[OperationCoverage]: ...

@dataclass(frozen=True)
class CaseInfo:
    node_id: str
    markers: tuple[str, ...]
    referenced_paths: tuple[str, ...]   # 自然语言注释里提到的接口路径
    covers: tuple[str, ...]             # 显式标注的 @pytest.mark.covers("opId")

def collect_cases(pytest_args: Sequence[str]) -> list[CaseInfo]: ...
    """通过 `pytest --collect-only -q --no-header` 的子进程拿到 nodeid；
    显式归属走 `@pytest.mark.covers(...)`（该 marker 需在 pytest.ini 注册）。"""
```

**诚实性红线**：覆盖矩阵不许靠猜。归属只有两个来源——显式 `covers` marker，
或用例源码里出现的接口路径字符串。二者都没有就算**未覆盖**，并在报告里如实标出。

## 六、目录归属（硬边界）

| 路径 | 归属 |
|---|---|
| `work/auto-test-demo/qaplat/**` | A 代理独占 |
| `work/auto-test-demo/testcases/platform/**` | A 代理独占 |
| `work/auto-test-demo/docs/platform-*.md` | A 代理独占 |
| `work/auto-test-demo/pytest.ini` | **仅允许追加 marker**（`platform`、`covers`） |
| `work/auto-test-demo/pyproject.toml` | **仅允许在 `[tool.mypy] files` 追加 `"qaplat"`** |
| `work/auto-test-demo/apis/**`、`common/**`、`mock_server/**`、`login_app/**`、`ui/**` | **禁止改动** |
| `work/auto-test-demo/testcases/{api,ui,mes,login,contract}/**` | **禁止改动** |

## 七、运行环境（实测，别再试探）

- 解释器：`C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`（3.12.10）
- 工作目录 `C:\Users\lefan\DSH_project\接口自动化_3\work\auto-test-demo`；沙箱只允许写本工作区之下。
- PowerShell 中文输出需先 `chcp 65001` 并设 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- 基线：`pytest --collect-only -q` = **363 tests collected**（HEAD `a49b097`，工作区干净）。
