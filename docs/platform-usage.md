# 平台层使用说明（qaplat）

本文回答三件事：**怎么跑**、**四层分别什么时候跑**、**数据库里存了什么**。

契约定义在 `qaplat/FROZEN_CLI.md`（子命令、退出码、导出签名）；本文只讲用法与口径，不重复签名。

---

## 0. 为什么包名是 `qaplat` 而不是 `platform`

`conftest.py:29-31` 会把仓库根插到 `sys.path[0]`。如果包名叫 `platform/`，它会**遮蔽标准库的
`platform` 模块**，pytest 一启动就崩（`ModuleNotFoundError` 或更隐蔽的 `AttributeError`）。
所以包名是 `qaplat`，目录名、`prog` 名、数据库名三者保持一致。

---

## 1. 前置：解释器与 PYTHONPATH

本仓库的 363 条基线用例里，`testcases/contract/test_schemathesis_properties.py` 依赖
**schemathesis**，而 schemathesis 并不在 venv 的 site-packages 里，它放在仓库外的
`work/pylibs` 目录（纯目录，未安装）。因此：

- 不带 PYTHONPATH 跑收集，只有 **175 条 + 1 个收集 ERROR**；
- 带上 PYTHONPATH 才是 **363 条**（再加平台层自测）。

```powershell
chcp 65001 > $null
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONPATH = 'C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'
cd C:\Users\lefan\DSH_project\接口自动化_3\work\auto-test-demo
$py = 'C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe'
```

`qaplat` 包本身**不**设置也不硬编码 PYTHONPATH——那是环境的责任，藏在包里会让人以为
`pip install -r requirements.txt` 就够。CI 里用真实 `pip install` 装 schemathesis，所以 CI 不需要这步。

---

## 2. 怎么跑

所有命令都是 `python -m qaplat.cli <子命令>`，均支持 `--help`。

`--db PATH` 是**全局选项，必须写在子命令之前**：

```powershell
& $py -m qaplat.cli --db qaplat/.cache/qaplat.db metrics show   # 正确
& $py -m qaplat.cli metrics show --db somewhere.db             # 报用法错误
```

默认库是 `qaplat/.cache/qaplat.db`，整个 `qaplat/.cache/` 已在 `.gitignore` 里——它是
「本机跑过什么」的历史状态，提交进去只会制造无意义 diff，还会让 CI 上的 flaky 检测
读到别人机器上的历史数据。

### 2.1 各子命令与退出码

| 命令 | 作用 | 退出码 |
| --- | --- | --- |
| `run --layer <层> [-- pytest 参数...]` | 按层拼 `-m` 表达式并调 pytest | **pytest 的退出码原样透传**（不落库） |
| `gate --layer <层>` | 跑该层 → **先落库** → 再比阈值 | `0` 达标 / `1` 未达标 / `2` 用法错误 |
| `registry build [--json PATH]` | 重建覆盖矩阵文件 | `0` |
| `registry gaps` | 打印未覆盖 operation | `0`（矩阵文件不存在时现场重建，不报错） |
| `metrics show [--last N]` | 最近 N 轮 + 分层聚合 + flaky 前 20 | `0` |
| `metrics flaky [--last N]` | 只看跨轮次不稳定用例 | `0` |
| `report [--out DIR]` | 生成静态 HTML 报表 | `0` |
| `drift record --commit <sha>` | 调 `scripts/export_openapi.py --check` 并落库 | `0` 无漂移 / `1` 有漂移 |

> `run` 与 `gate` 的区别：`run` 只是「换个方式跑 pytest」，适合本地开发；`gate` 会**落库**
> 并**判阈值**，是流水线该调的那个。落库顺序是「先写库再判定」，所以**门禁失败的那一轮
> 也会留下记录**——否则你只能看到红灯，看不到红灯前一轮是什么样。

### 2.2 最常用的一组

```powershell
& $py -m pytest -m platform -q                    # 平台层自测
& $py -m qaplat.cli gate --layer smoke            # 冒烟门禁
& $py -m qaplat.cli registry gaps                 # 覆盖缺口
& $py -m qaplat.cli metrics show                  # 历史度量
& $py -m qaplat.cli report --out qaplat/.cache/report
& $py -m qaplat.cli drift record --commit (git rev-parse --short HEAD)
```

报表生成后用浏览器打开 `qaplat/.cache/report/index.html`（五个页面：总览 / 轮次 /
覆盖 / 归因 / 漂移）。页面是纯标准库生成的静态 HTML，**不引 CDN、不引 JS 框架**，
离线打开即可。

---

## 3. 四层门禁：什么时候跑哪一层

四层定义在 `qaplat/gate.py` 的 `LAYERS`，表达式是**逐字冻结**的（改一个字，例如把
`not ui` 写成 `not_ui`，不会报错，只会静默地把该层变成「跑 0 条、全绿」——这是最危险
的一类失效，所以 `testcases/platform/test_gate_layers.py` 把四个表达式逐字钉死）。

| 层 | `-m` 表达式 | 选中的是 | 何时跑 | 阈值 `min_pass_rate` | 时长预算 |
| --- | --- | --- | --- | --- | --- |
| `smoke` | `smoke` | 打了 `smoke` 标记的 36 条 | 每次提交前 / 本地改完随手跑 | `1.00` | ≤ 90s |
| `pr` | `smoke or api or platform` | 冒烟 + 接口 + 平台层自身 | 开 PR / 合并前 | `1.00` | ≤ 450s |
| `nightly` | `not ui` | 除 UI 之外的全部 | 每日定时（UI 依赖浏览器，夜间机器不一定有） | `0.98` | ≤ 1200s |
| `release` | 空（**不带 `-m`** = 全量） | 全部，含 UI | 发版前 | `0.95` | ≤ 2400s |

`timeout_seconds`（600 / 900 / 1800 / 2700）是**硬杀线**：超时按 `exit_code=124` 处理，
按未达标算。`max_duration_seconds` 是**预算线**：超了记进 `violations`，但不改退出码语义。

### 3.1 通过率的分母是「有结论的用例」

`pass_rate = passed / (passed + failed)`。**`error` 不进分母。**

理由是本机现实：`testcases/ui/test_login.py` 的 6 条用例在没有 Playwright 浏览器的机器上
必然 `error`（`Executable doesn't exist`），而它们**带 `smoke` 标记**。若把 error 算进分母，
`smoke` 层会在这台机器上**常年红灯**，红灯一多就没人看了——门禁的价值在于它红了就一定是
真的有事。

代数是：本机 `smoke` 层 `total=36 passed=30 failed=0 errors=6` → `pass_rate=100.0%` → **达标**。
但 `errors=6` 会原样打印在 `gate` 输出里，**不隐藏**：误差是被允许的，不是被掩盖的。

### 3.2 会算未达标的四种情况

写进 `GateResult.violations`：

1. `total == 0` —— 一条都没跑到（表达式写错、标记打错、路径变了都会这样）；
2. `passed + failed == 0` —— 全是 error/skip，没有有结论的用例；
3. `pass_rate < min_pass_rate`；
4. `duration_seconds > max_duration_seconds`。

第 1、2 条专门用来堵「静默全绿」：跑 0 条**不是**通过。

---

## 4. 覆盖矩阵：归属只有两个来源

`registry` 回答「哪个 operationId 被哪些用例覆盖」。这不看契约本身，而是看**用例源码**。

**诚实性红线**：归属只允许来自这两个来源，都不算「猜」——

1. **显式 marker**：`@pytest.mark.covers("login_api_v1_auth_login_post")`（`covers` 已注册进 `pytest.ini`）。
   marker 的 **args 不会进入任何 pytest 报告**，事后无从恢复，所以由采集插件
   `qaplat/_dump.py` 在 `pytest_collection_modifyitems` 阶段当场取走。
2. **用例源码里的接口路径字面量**：正则 `(?<![\w.])(/api[A-Za-z0-9_\-./{}]*)`，扫的是
   `__pycache__` 之外的 `testcases/**.py`。

两个来源都没有 → **算未覆盖，并在报表里如实标成 `GAP`**。没有第三条路：不靠函数名猜、不靠
断言文本猜、不靠「这个文件看起来像在测库存」猜。

### 4.1 计数口径（重要，否则会读错）

归属的粒度是**文件级**：文件里的路径字面量属于该文件里的**每一条**用例。

所以 `testcases/contract/test_schemathesis_properties.py`（模块级
`ALL_OPERATIONS` + `pytestmark = pytest.mark.contract`，参数化展开成 188 条用例）会让它有
引用的那几个 operation 各挂上上百条 nodeid。看到「188 条用例覆盖 issue 接口」不要理解成
「有 188 个独立场景在测它」，正确理解是「**1 个文件（188 条参数化用例）引用了这个路径**」。

为此 CLI 的归属详情按文件归组显示：`188 条用例 / 1 个文件`。`registry.owner_files()` 提供
这个视图。

### 4.2 已知的真实缺口（不是 bug）

`testcases/api/**` 的 42 条用例走的是 `apis/` 封装对象，源码里**没有任何 `/api` 字面量**，
所以在来源 2 下它们一条归属都拿不到。这是预期行为，也是这份矩阵最该暴露的事实：**接口
用例没有把「我在测哪个 operationId」说出来**。补的办法就是给这些用例打 `covers` marker。

---

## 5. 数据库 schema

纯 `sqlite3`，无 ORM。`check_same_thread=False`（pytest-xdist 各 worker 可能各自持有连接），
`row_factory = sqlite3.Row`。schema 版本写在 `PRAGMA user_version`，当前 `SCHEMA_VERSION = 1`。
`migrate()` 幂等（全部 `CREATE ... IF NOT EXISTS`），每次开库都会调。

```sql
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,          -- "<layer>-<UTC 时间戳>-<随机后缀>"
    layer             TEXT NOT NULL,             -- smoke / pr / nightly / release
    commit_sha        TEXT,                      -- NULL 表示本地未取到 git sha
    started_at        TEXT NOT NULL,             -- ISO 8601 带 +00:00
    duration_seconds  REAL NOT NULL,
    total             INTEGER NOT NULL,
    passed            INTEGER NOT NULL,
    failed            INTEGER NOT NULL,
    errors            INTEGER NOT NULL,
    skipped           INTEGER NOT NULL,
    xfailed           INTEGER NOT NULL,
    pass_rate         REAL NOT NULL,             -- passed / (passed + failed)
    outcome           TEXT NOT NULL              -- passed / failed / error
);

CREATE TABLE IF NOT EXISTS case_outcomes (
    run_id            TEXT NOT NULL,
    case_id           TEXT NOT NULL,             -- pytest nodeid
    outcome           TEXT NOT NULL,             -- passed/failed/error/skipped/xfailed
    duration_seconds  REAL NOT NULL,
    PRIMARY KEY (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS contract_drift (
    commit_sha        TEXT PRIMARY KEY,
    checked_at        TEXT NOT NULL,
    drifted           INTEGER NOT NULL,          -- 0 / 1（sqlite 无 bool）
    changed           TEXT NOT NULL              -- JSON 数组文本
);

CREATE INDEX IF NOT EXISTS idx_case_outcomes_case ON case_outcomes (case_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
```

### 5.1 三张表各自的用途

- **`runs`**：每轮一行的汇总。`metrics show` 与报表「轮次」页读它。
- **`case_outcomes`**：每轮每用例一行。**flaky 检测的唯一依据**——
  `flaky_cases(last_n_runs)` 在最近 N 轮里按 `case_id` 聚合，只保留
  `outcome IN ('passed','failed')`（error/skip 不算「结论」），条件 `HAVING 通过轮次 > 0
  AND 通过轮次 < 出现轮次`，即**既通过过也失败过**。返回 `(case_id, 通过轮次, 出现轮次)`。
  单轮全绿时它恒为空——那个空**不代表用例稳定**，只代表样本还不够，`metrics flaky`
  会把这句话主动打出来。
- **`contract_drift`**：`commit_sha` 做主键，同一 commit 重跑是 `INSERT OR REPLACE`，
  不会堆积重复行。漂移判定完全交给 `scripts/export_openapi.py --check`（逐字节比对），
  `qaplat` 不做二次判断。

### 5.2 `qaplat/.cache/` 里还有什么

| 路径 | 内容 | 谁写 |
| --- | --- | --- |
| `qaplat.db` | 上表三张表 | `gate` / `drift record` |
| `registry.json` | 覆盖矩阵（`operations_total` / `cases_total` / `covered` / `coverage_ratio` / `coverage[]`） | `registry build` |
| `raw/<layer>.json` | 该层原始采集结果（用例结论、markers、源文件、失败报文） | `gate` → `qaplat/_dump.py` |
| `report/*.html` | 五个静态页面 | `report` |

`raw/<layer>.json` 里的失败报文（`message`，截断 2000 字符）是**归因分类的唯一证据来源**：
pytest 的报告文本不落盘就再也拿不回来，所以采集期必须抓。

---

## 6. 失败归因

`qaplat/classify.py` 把失败分到五类：`ENVIRONMENT` / `DATA` / `DEFECT` / `REGRESSION` / `UNKNOWN`。
判定顺序是 **环境 → 数据 → 缺陷 → 其余**，`error` 且不带环境特征时降级为 `UNKNOWN`，
最后才落到 `REGRESSION`（真回归）。

这道顺序的原因是：**把环境问题误报成回归，比漏报更贵**。`Executable doesn't exist`、
`ModuleNotFoundError`、`Connection refused`、`\b50[234]\b` 这类证据一出现就先判环境，
否则「没装浏览器」会被反复当成「代码挂了」，报警很快就没人信了。

分类结果在报表 `attribution.html` 里，`index.html` 的总览卡片显示的是**真回归条数**
（`REGRESSION`），因为那才是需要立刻处理的一栏。
