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

| 层 | `-m` 表达式 | 目标时长 | `min_pass_rate` | `max_errors` |
|---|---|---|---|---|
| smoke | `smoke` | < 1 min | 1.0 | 0 |
| pr | `smoke or api or platform` | 3–5 min | 1.0 | 0 |
| nightly | `not ui` | 全量非 UI | 0.98 | 3 |
| release | `` （空，全量，含 ui） | 含 UI | 0.95 | 5 |

`max_errors` 是 2026-10 补的**第五条 blocking 判据**，堵的是这个缺口：`evaluate()` 原先只看
`decisive = passed + failed`，`decisive == 0` 才拦，于是「36 条里 30 条通过、6 条 error」算出
`pass_rate = 100%`、门禁报达标——**没跑成的用例被静默当成通过**。取值与 `min_pass_rate` 同一套
松紧梯度：阻塞层（smoke/pr）一条都不许 error，全量层（nightly/release）留少量环境抖动余量
（夜间/发布跑 `-m "not ui"` 与全量，浏览器与网络抖动不属于被测回归）。

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
    max_errors: int  # 本轮允许的 error 条数上限，超出即不达标


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

**自测排除（2026-10 补）**：`testcases/platform/**` 是**平台自身的单元测试**，不参与路径归属。
理由：那里出现的 `/api/v1/...` 是喂给提取器/矩阵函数的**输入样本**（例如断言
`client.get("/api/v1/materials")` 能被认成 `create_material_api_v1_materials_post`），
不是对被测服务的调用；让它参与归属，等于矩阵宣称一个从不调用该接口的文件覆盖了它——
比漏报更危险，正好撞在上面的红线上。实现口径：`read_referenced_paths()` 对
`ROOT / "testcases" / "platform"` 前缀下的文件返回空元组（**返回空元组**，不是抛异常；
`covers` marker 仍然生效，平台层用例若真要声明归属，显式写 marker 即可）。
判定用 `Path` 归一化后的前缀比较，不靠字符串 `startswith`——否则
`testcases/platform_extra/` 会被误伤。该规则要有平台层单测钉住（正例 + 反例各一条）。

## 六、目录归属（硬边界）

| 路径 | 归属 |
|---|---|
| `work/auto-test-demo/qaplat/**` | A 代理独占 |
| `work/auto-test-demo/testcases/platform/**` | A 代理独占 |
| `work/auto-test-demo/docs/platform-*.md` | A 代理独占 |
| `work/auto-test-demo/pytest.ini` | **仅允许追加 marker**（`platform`、`covers`） |
| `work/auto-test-demo/pyproject.toml` | **仅允许在 `[tool.mypy] files` 追加 `"qaplat"`** |
| `work/auto-test-demo/apis/**`、`common/**`、`mock_server/**`、`login_app/**`、`ui/**` | **禁止改动** |
| `work/auto-test-demo/testcases/{api,ui,mes,login,contract}/**` | **禁止改动**（例外：只允许**追加** `@pytest.mark.covers("opId")` 标注，2026-10 获授权，见下） |

**`covers` 追加授权（2026-10）**：覆盖矩阵对 MES/UI 侧严重低估——那些用例走 `apis/` 封装对象，
源码里只写相对路径（`f"/work-orders/{order_no}/approve"`，`/api/v1` 前缀在 base_url 里），
而提取器只认 `/api` 开头的字面量，于是 15 个 GAP 里有 10 个其实被真实调用却显示为未覆盖。
`registry.py:13-15` 早已写明正解是「在用例上加 `covers` marker」。故本表对
`testcases/{api,ui,mes,login,contract}/**` 的禁令开一个**唯一例外**：允许追加
`@pytest.mark.covers(...)` 装饰器，**不得**改动任何断言、fixture、控制流或 import。
不加宽提取器的推断能力（那会破坏诚实性红线）。

## 七、运行环境（实测，别再试探）

- 解释器：`C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`（3.12.10）
- 工作目录 `C:\Users\lefan\DSH_project\接口自动化_3\work\auto-test-demo`；沙箱只允许写本工作区之下。
- PowerShell 中文输出需先 `chcp 65001` 并设 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- 基线：`pytest --collect-only -q` = **363 tests collected**（HEAD `a49b097`，工作区干净）。
