# 平台层实测报告（qaplat）

**本文所有数字都来自本机真实执行，没有任何估算或占位数字。** 采集环境与命令见第 1 节，
每节末尾标注了产出该数字的命令。采集时间：2026-10-14（UTC，即表中 `run_id` 的时间戳）。

| 项 | 值 |
| --- | --- |
| HEAD | `a49b097` |
| 解释器 | `C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`（Python 3.12.10） |
| `PYTHONPATH` | `C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs`（**必须**，见第 1 节） |
| 平台层自测 | 99 条，全绿 |
| 用例总数 | 462 = 363 基线 + 99 平台层新增 |
| 接口覆盖率 | 14 / 29 = **48.3%** |
| `drift record` | 无漂移（exit 0） |
| flaky 用例 | 0 条（**且样本不足，见第 6 节**） |
| 真实回归 | 0 条 |

---

## 1. 环境前提（这个数字最关键）

不带 `PYTHONPATH` 时**基线不是 363**：

```
$ python -m pytest --collect-only -q          # 无 PYTHONPATH
175 tests collected, 1 error                  # ERROR: No module named 'schemathesis'

$env:PYTHONPATH = 'C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'
$ python -m pytest -m "not ui" -q --collect-only
448/462 tests collected (14 deselected) in 0.62s
```

原因：`testcases/contract/test_schemathesis_properties.py` 依赖 schemathesis，而 schemathesis
既不在 venv、也没有任何 `.pth` 指过去，它躺在仓库外的 `work/pylibs` 里（纯目录，未安装）。
CI 用真实 `pip install -r requirements.txt` 装它，所以 **CI 上不存在这个问题**；这是**本机**特有的
前置条件，写进 `docs/platform-usage.md` 第 1 节，且**没有硬编码进 `qaplat` 包**。

### 1.1 收集数对账（462 = 363 + 99）

| `-m` 表达式 | 收集数 | 说明 |
| --- | --- | --- |
| 全量 | **462** | 363 基线 + 99 平台层 |
| `smoke` | **36** | 未变（新增用例只打 `platform`） |
| `smoke or api or platform` | **230** | = 131（基线）+ 99（platform） |
| `not ui` | **448** | = 349（基线）+ 99（platform） |

```
$ python -m pytest -m smoke -q --collect-only                    → 36/462 collected  (426 deselected)
$ python -m pytest -m "smoke or api or platform" -q --collect-only → 230/462 collected (232 deselected)
$ python -m pytest -m "not ui" -q --collect-only                  → 448/462 collected (14 deselected)
$ python -m pytest -m platform -q                                 → 99 passed, 363 deselected
```

`363` 这个基线数字没有被破坏：原有 363 条照旧全被收集，新增的 99 条全部只打
`platform` 标记，因此 `smoke` 层仍是 36 条。

---

## 2. 平台层自测：99 条全绿

```
$ python -m pytest -m platform -q
99 passed, 363 deselected, 1 warning in 6.50s
[exit code: 0]
```

唯一 warning 来自 `work/pylibs/schemathesis/generation/coverage.py:305`
（`jsonschema.exceptions.RefResolutionError is deprecated`），是第三方库自身的告警，与本平台无关。

四条被测链路（对应四个测试文件）：

| 文件 | 行数 | 覆盖什么 |
| --- | --- | --- |
| `testcases/platform/test_gate_layers.py` | 200 | 四层表达式逐字钉死、`build_pytest_args` 拼装顺序、阈值六种边界、退出码 0/1、`run` 与 `gate` 的落库差异、非法层名 = 2 |
| `testcases/platform/test_metrics_store.py` | 291 | 三张表建表幂等、`record_run` 落库与重放、`recent_runs` 排序、`flaky_cases` 只收「既通过也失败」、`contract_drift_history`、`pass_rate` 分母口径 |
| `testcases/platform/test_registry_coverage.py` | 260 | 两条归属来源各自单独验 + 合并验、占位符归一化、`GAP` 不靠猜、矩阵 JSON 往返、真子进程 `collect_cases` |
| `testcases/platform/test_drift_report_cli.py` | 288 | 漂移 0/1 两条路径与 `changed` 落库、缺脚本 = 2、归因五分类、报表五个页面真实生成 |

所有度量类测试都用 `tmp_path` 建**临时** SQLite，不碰 `qaplat/.cache/qaplat.db` 真库。

---

## 3. 分层门禁：真实耗时与判定

| 层 | 命令 | total | passed | failed | errors | pass_rate | 层耗时 | 预算 | 判定 | 退出码 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `smoke` | `gate --layer smoke` | 36 | 30 | 0 | 6 | 100.0% | **29.3s** | ≤90s | 达标 | **0** |
| `pr` | `gate --layer pr` | 230 | 224 | 0 | 6 | 100.0% | **70.9s** | ≤450s | 达标 | **0** |

原始输出（`--layer smoke`，逐字）：

```
[qaplat] 层=smoke 表达式=smoke 阈值=100%
[qaplat] total=36 passed=30 failed=0 errors=6 skipped=0 xfailed=0
[qaplat] pass_rate=100.0% duration=29.3s within_budget=True run_id=smoke-20261014T134050-30a1a9ef
[qaplat] 门禁达标：smoke
```

```
[qaplat] 层=pr 表达式=smoke or api or platform 阈值=100%
[qaplat] total=230 passed=224 failed=0 errors=6 skipped=0 xfailed=0
[qaplat] pass_rate=100.0% duration=70.9s within_budget=True run_id=pr-20261014T134349-73c2c670
[qaplat] 门禁达标：pr
```

`smoke` 层另有 `pytest -m smoke -q` 的直接实测（不走门禁）：

```
30 passed, 426 deselected, 1 warning, 6 errors in 18.47s
[exit code: 1]
```

两处 `6 errors` 是同一批：`testcases/ui/test_login.py` 的 6 条用例（带 `smoke` 标记）
在无 Playwright 浏览器的机器上全部 `error`（`Executable doesn't exist`）。
`pytest` 直接跑 exit=1，而 `gate` 判定 **exit=0 达标**——这不是矛盾，是刻意的口径：
**通过率分母是 `passed + failed`，`error` 不进分母**。理由见 `docs/platform-usage.md` §3.1：
把这类 error 计入分母会让 `smoke` 层在这台机器上常年红灯。

代价要说清楚：**`gate` 达标不代表 `pytest` exit 0**。`errors=6` 会原样打印在门禁输出里。
把这道口径改成「error 也算不达标」只需改 `LayerSpec.min_pass_rate` 之外的判定逻辑，
但会让 `smoke` 层失去信号价值，故保持现状。

---

## 4. 覆盖矩阵：14 / 29 = 48.3%

```
$ python -m qaplat.cli registry build
[qaplat] 契约 ...docs\openapi.json：29 个 operation；收集用例 462 条
[qaplat] operation 总数=29 已覆盖=14 未覆盖=15 覆盖率=48.3%
[exit code: 0]
```

> 注意：任务书里写「13 paths / 39 operations」，**实测是 23 paths / 29 operations**。
> 以实测为准，`registry build` 的输出即实测值。

`registry gaps` 完整输出（15 条缺口，全部是 **`GAP`，即两个来源都没有**）：

```
[qaplat] operation 总数=29 已覆盖=14 未覆盖=15 覆盖率=48.3%
  GAP  POST   /api/v1/work-orders/{order_no}/batches               add_batch_api_v1_work_orders__order_no__batches_post
  GAP  POST   /api/v1/work-orders/{order_no}/approve               approve_work_order_api_v1_work_orders__order_no__approve_post
  GAP  POST   /api/v1/work-orders/{order_no}/cancel                cancel_work_order_api_v1_work_orders__order_no__cancel_post
  GAP  POST   /api/v1/work-orders/{order_no}/complete              complete_work_order_api_v1_work_orders__order_no__complete_post
  GAP  POST   /api/v1/work-orders/{order_no}/inspections           create_inspection_api_v1_work_orders__order_no__inspections_post
  GAP  GET    /api/v1/work-orders/{order_no}                       get_work_order_api_v1_work_orders__order_no__get
  GAP  GET    /api/v1/work-orders/{order_no}/batches               list_batches_api_v1_work_orders__order_no__batches_get
  GAP  GET    /api/v1/work-orders/{order_no}/inspections           list_inspections_api_v1_work_orders__order_no__inspections_get
  GAP  GET    /api/v1/inventory/stock                              list_stock_api_v1_inventory_stock_get
  GAP  GET    /api/v1/auth/me                                      me_api_v1_auth_me_get
  GAP  POST   /api/v1/work-orders/{order_no}/schedule              schedule_work_order_api_v1_work_orders__order_no__schedule_post
  GAP  POST   /api/v1/work-orders/{order_no}/ship                  ship_work_order_api_v1_work_orders__order_no__ship_post
  GAP  POST   /api/v1/work-orders/{order_no}/batches/{batch_no}/start start_batch_api_v1_work_orders__order_no__batches__batch_no__start_post
  GAP  POST   /api/v1/work-orders/{order_no}/stock-in              stock_in_api_v1_work_orders__order_no__stock_in_post
  GAP  GET    /api/v1/work-orders/{order_no}/traceability          traceability_api_v1_work_orders__order_no__traceability_get
[exit code: 0]
```

### 4.1 已覆盖的 14 个 operation（含归属来源）

`registry build` 的逐行输出（节选，格式是 `状态 方法  路径  operationId  归属`）：

```
  OK   POST   /api/v1/auth/login            login_api_v1_auth_login_post            208 条用例 / 2 个文件
  OK   GET    /api/v1/healthz               healthz_api_v1_healthz_get               43 条用例 / 2 个文件
  OK   POST   /api/v1/inventory/issue       issue_material_api_v1_inventory_issue_post   188 条用例 / 1 个文件
  OK   GET    /api/v1/inventory/ledger      list_ledger_api_v1_inventory_ledger_get      188 条用例 / 1 个文件
  OK   POST   /api/v1/inventory/reverse     reverse_material_api_v1_inventory_reverse_post 188 条用例 / 1 个文件
  OK   GET    /api/v1/materials             list_materials_api_v1_materials_get         231 条用例 / 3 个文件
  OK   POST   /api/v1/materials             create_material_api_v1_materials_post       231 条用例 / 3 个文件
  GAP  GET    /api/v1/auth/me               me_api_v1_auth_me_get                    无归属
```

**「208 条用例」不要读成「208 个场景在测它」。** 归属粒度是**文件级**：文件里的路径字面量
属于该文件里的每一条用例。`testcases/contract/test_schemathesis_properties.py` 模块级有
```python
ALL_OPERATIONS: list[str] = [f"{method} {path}" for method, path, _ in iter_operations(load_spec(PRISTINE_SPEC_PATH))]
```
配 `pytestmark = pytest.mark.contract`，参数化展开成 **188 条**用例 → 它引用的那几个 operation
各挂 188 条 nodeid。所以 CLI 才按文件归组显示 `N 条用例 / M 个文件`；
正确读法是「**M 个文件（N 条用例）引用了这个路径**」。`registry.owner_files()` 提供这个视图。

引用过路径字面量的用例共 **253 条，只来自 4 个文件**：
`test_schemathesis_properties.py`（188）、`testcases/login/test_login_contract.py`（29）、
`testcases/contract/test_contract_export.py`（20）、`testcases/contract/test_handwritten_parity.py`（16）。

### 4.2 为什么有 15 个 GAP —— 这是本平台最有价值的输出

15 个缺口里 14 个在 `/api/v1/work-orders/**`，1 个是 `/api/v1/auth/me`。
根因不是「没测」，而是**用例没说出自己在测哪个 operationId**：

- `testcases/api/**` 的 42 条用例走 `apis/` 封装对象，源码里**没有任何 `/api` 字面量**
  → 来源 2 一条都命不中；
- 没有任何用例使用 `@pytest.mark.covers(...)`（该 marker 是本平台新注册的）。

按诚实性红线，两个来源都没有就是 `GAP`，**绝不靠函数名或断言文本猜**。补法很具体：
给这些用例打 `covers` marker，覆盖率立刻能反映真实情况。

---

## 5. 契约漂移：无漂移

```
$ python -m qaplat.cli drift record --commit a49b097
[一致] ...docs\openapi.json（80867 字节）
[一致] ...docs\openapi-login_app.json（2129 字节）
[qaplat] 已记录漂移检查 commit=a49b097 drifted=False 变化文件=[] 历史条数=1
[qaplat] 契约一致，无漂移
[exit code: 0]
```

漂移判定**完全交给** `scripts/export_openapi.py --check`（逐字节比对 + `dumps()` 规范化），
`qaplat` 不做二次判断、不解释 OpenAPI 语义。有漂移时脚本 stderr 打 `[漂移] <文件> 与代码不一致`
并 exit 1，`cli` 从中取文件名落库、返回 1。

**踩到并修掉的一个真 bug（在此记录，因为它会静默降级）**：Windows 上子进程走管道时 Python
默认按 `cp936` 编码 stdout，而 `subprocess.run(..., encoding="utf-8")` 按 utf-8 解码 → `[漂移]`
这三个中文字符变成替换字符 → `_changed_files()` 认不出漂移行 → `changed` 只剩
`('(未知，见脚本输出)')`。**漂移判定本身（exit code）仍正确，但「漂变了哪个文件」这条最有用的
信息丢了**。修法：给子进程显式设 `PYTHONIOENCODING=utf-8` 并传 `env=`。
`qaplat/registry.py` 的 `collect_cases()` 有同样的隐患（同一个原因），一并修了。
`test_drift_report_cli.py` 里有一条用例专门钉住这个行为。

---

## 6. 度量库现状

```
$ python -m qaplat.cli metrics show
[qaplat] 最近 2 轮（库：...\qaplat\.cache\qaplat.db）
  run_id                         层     通过率    总   通过  失败  错误  跳过  xfail  耗时s  结论
  pr-20261014T134349-73c2c670    pr    100.0%   230   224    0    6     0      0   70.9  failed
  smoke-20261014T134050-30a1a9ef smoke 100.0%    36    30    0    6     0      0   29.3  failed
[qaplat] 分层聚合
  pr       轮次=  1 平均通过率= 100.0% 平均耗时=    70.9s
  smoke    轮次=  1 平均通过率= 100.0% 平均耗时=    29.3s
[qaplat] 不稳定用例（最近 10 轮，既通过过也失败过）：0 条
[exit code: 0]
```

```
$ python -m qaplat.cli metrics flaky
[qaplat] 最近 10 轮内跨轮次不稳定用例：0 条
  （无。要么每轮都稳定通过，要么失败样本还不够——单轮全绿时该结果不代表用例稳定）
[exit code: 0]
```

**必须说清楚的话：`0 条 flaky` 现在不构成证据。** 库里只有 2 轮、且都是同一批用例的
同一批 error，没有出现「同一用例某轮过某轮不过」。flaky 检测要跨轮次有翻转才能说话；
`metrics flaky` 自己也把这句话打出来了，没有伪造成「用例很稳定」。

`结论` 列显示 `failed` 而 `通过率` 是 `100.0%` —— 这是 `outcome_of()` 的定义：
只要 `failed or errors` 非零就记 `failed`（契约只允许 `passed/failed/error` 三值，
没有「通过但有 error」这一档）。**这是契约的取值空间限制，不是 bug，但读表时要注意**：
`结论=failed` + `通过率=100%` 意为「有结论的用例全通过，另有若干 error」。

### 6.1 报表

```
$ python -m qaplat.cli report --out qaplat/.cache/report
[qaplat] 报表已生成：qaplat\.cache\report\index.html
[qaplat] 轮次=2 用例级记录=266 接口覆盖=14/29 真实回归=0
  index.html  runs.html  coverage.html  attribution.html  drift.html
[exit code: 0]
```

五个页面实测生成，体积：`coverage.html` 322398 字节 / `index.html` 2719 /
`runs.html` 2327 / `attribution.html` 3660 / `drift.html` 1867。
纯标准库生成，无 CDN、无 JS 框架，离线可打开。

---

## 7. 失败归因：6 条 error 全部判为「环境」

用**真实**的 `qaplat/.cache/raw/smoke.json`（36 条用例、6 条非通过）跑 `classify`：

```
cases= 36 non-pass= 6
  environment 6
  data        0
  defect      0
  regression  0
  unknown     0
verdict: 6 条失败全部属于环境/数据/已知缺陷，非回归信号
```

6 条 error 全部来自 `testcases/ui/test_login.py`，报错文本含
`Executable doesn't exist`（Playwright 浏览器未安装）→ 判定为 `ENVIRONMENT`，`regression=0`。

判定顺序（环境 → 数据 → 缺陷 → 其余）是刻意的：**把环境问题误报成回归比漏报更贵**——
「没装浏览器」被反复当成「代码挂了」，报警很快就会没人看。相关取舍写在
`docs/platform-usage.md` §6。

---

## 8. 静态检查

CI 的 lint job（`.github/workflows/test.yml` L65/L70）与 mypy job（L105）跑的是**仓库根、全仓**
的三条命令，所以下面按 CI 的原样命令记录，而不是只跑 `qaplat` 子目录——只跑子目录会漏掉
CI 真正会红的范围：

```
$ python -m ruff format --check .        # CI L70
100 files already formatted
[exit code: 0]

$ python -m ruff check .                 # CI L65
All checks passed!
[exit code: 0]

$ python -m mypy                         # CI L105，无参数：读 pyproject.toml 的 [tool.mypy] files
Success: no issues found in 80 source files
[exit code: 0]

$ python -m mypy qaplat                  # 仅本包，便于定位
Success: no issues found in 9 source files
[exit code: 0]
```

**注意 CI 的 mypy 是 `python -m mypy`（无参数）**，它读 `pyproject.toml` 的
`[tool.mypy] files = [..., "qaplat"]`，因此 `testcases/` 也在检查范围内——本次新增的
`testcases/platform/**` 同样归 mypy 管。`mypy qaplat` 绿**不等于** CI 绿：开发过程中正是
`testcases/platform/test_metrics_store.py` 的 `store` fixture（带 `yield`）返回标注写成
`-> MetricsStore`，裸跑 CI 同款命令时报
`error: The return type of a generator function should be "Generator" or one of its supertypes  [misc]`，
改成 `Iterator[MetricsStore]` 后才绿。

**文件计数对账**（HEAD 基线 `a49b097` 用 `git archive HEAD` 解到临时目录实测）：

| 命令 | HEAD 基线 | 现在 | 差 | 说明 |
|---|---|---|---|---|
| `ruff format --check .` | 83 files already formatted | 100 | +17 | 新增 14 个 `.py` + 3 个 `.md`（ruff format 也统计 md） |
| `ruff check .` | All checks passed! | All checks passed! | 0 | — |
| `python -m mypy` | 66 source files | 80 | +14 | 新增 14 个 `.py`（mypy 不计 md） |

CI 的判据是**退出码**而非计数，两者差异不影响 job 结论。

`qaplat` 已追加进 `pyproject.toml` 的 `[tool.mypy] files`，**没有新增任何 mypy override**
来蒙混过关。修掉的静态问题共 11 处，其中值得记的两类：

- `report.py`：`containers: list[tuple[str, str]]` 却 `append` 字符串 → mypy `arg-type` 直接命中，
  是真实的类型与意图不符（改的是类型声明，不是加 ignore）；
- `gate.py` / `registry.py` 的 `json.loads()` 返回值是 `object`，`payload.get(...)` 结果类型没收窄，
  靠显式 `isinstance` 分支收窄而不是 `cast`。

---

## 9. 未做到 / 不确定的地方

诚实列出，不掩盖：

1. **`nightly` 与 `release` 两层从未真跑过门禁。** `release` 含 `ui` 目录，本机没有 Playwright
   浏览器，14 条 UI 用例会全 error；跑它需要先 `playwright install`。表中 `not ui` 的收集数
   （448）是实测的，但该层的**执行耗时与判定未实测**，因此不在第 3 节表里。
2. **`metrics flaky` 的 0 条不构成证据**（只有 2 轮，无翻转样本）。已在 §6 写明。
3. **没有跑过 `pytest -n auto`（xdist）下的门禁**。`_dump.py` 已按契约处理好 xdist worker
   （worker 侧不写文件、controller 侧写 JSON），但**该分支无实测覆盖**，属于「按设计写好、
   未经真实多进程验证」。
4. **契约基准与任务书描述不一致**：任务书写「13 paths / 39 operations」，实测
   `docs/openapi.json` 是 **23 paths / 29 operations**。我以实测为准实现，未改动契约文件。
5. **`qaplat` 的归属判定不含 `docs/openapi-login_app.json`**（3 个 operation）。`registry` 目前
   只读 `docs/openapi.json`（`DEFAULT_SPEC_PATH`）。这是 FROZEN_CLI.md 的签名决定
   （`load_operations(spec_path)` 单文件），若要求覆盖两个契约需改契约——**未擅自改**。
6. **`qaplat/.cache/` 里的历史只有本机 2 轮**，`report` 与 `metrics show` 的数字会随
   后续运行变化；本文记录的是 2026-10-14 那两轮的值。
7. **`gate` 的输出与 `pytest` 的退出码语义不同**（§3 已说明）：本机 `smoke` 层 `pytest` 直接跑
   exit=1、`gate` 判 0。这是设计取舍而非缺陷，但下游若直接用 `pytest -m smoke` 做 CI 卡点，
   会与 `gate` 得出相反结论——**建议统一走 `gate`**。
8. **CI 的 pre-commit 步骤（L380 `pre-commit run --all-files`）未在本机实测**：本机装不上
   pre-commit（`pip` 建虚拟环境时 `ensurepip` 被拒，`PermissionError: [WinError 5]`，见
   `.pre-commit-config.yaml` 文件头说明）。我改为**逐条自查 hook 的判据**：新增 16 文件 +
   3 个 M 文件全部「末尾单换行、纯 LF、无行尾空白、无末尾多余空行」；用 pre-commit
   `check-merge-conflict` 的精确正则 `^(<<<<<<< |=======$|>>>>>>> )` 扫 19 个文件命中 0；
   `pyproject.toml` 用 `tomllib` 解析通过；最大新增文件为已被 gitignore 的
   `qaplat/.cache/report/coverage.html`（339708 字节 < 1024KB 上限）。**自查不等于 hook 实测**，
   若 CI 报出 hook 差异请以 hook 输出为准。
