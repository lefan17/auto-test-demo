# 波 2 · P1-3 覆盖率盲区（子进程服务） — 交接文档

> 基线：`44fc60d`（波 1 收口：口令、契约与实测数字同步到 README 与 CHANGELOG）
> 工作仓库：`C:\Users\lefan\DSH_project\接口自动化_3\work\w2-coverage`（原仓库 `DH_1\auto-test-demo` 只读参考，未改动）
> 解释器：`C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`（Python 3.12.10，coverage 7.16.2 / pytest-cov 6.0.0）
> 每次跑 pytest/ruff/mypy 前设：`$env:PYTHONPATH='C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'`

面向整合者。本文件只写证据：改了哪些文件、命令输出原文、未解决项。

> 关于本文件里出现的 `work_tmp/...` 路径：那是本次改动期间的取证目录，**已在提交前整体删除**
> （见 §12）。文中凡引用 `work_tmp/final6/*.txt` 之类的地方，都是当时原始输出的来源标注；
> 关键行已逐字摘录进本文件，重跑 §7 的命令即可复现，不需要那个目录。

原始病灶定义（波划分原文）：

> **覆盖率盲区**：`mock_server/*` 的覆盖率恒为 0%（约 1358 条 statements），方向=子进程覆盖率
> （`coverage run --parallel-mode` + `combine`）。

---

## 1. 改动清单（改了什么、为什么）

### 新增

| 文件 | 行数 | 为什么 |
|---|---|---|
| `scripts/service_launcher.py` | 69 | 被测服务的启动垫片：先装「吸收信号」的 handler，再 `runpy.run_module(<服务模块>, run_name="__main__")`。它是**让覆盖率不丢数据**的那一环（原因见 §2），且对被测服务完全透明——覆盖率不该要求被测对象改代码。 |
| `testcases/mes/test_coverage_collection.py` | 85 | 元测试：自己起一个短命服务、自己注入覆盖率环境变量、断言「分片真的产出**且**里面有 `mock_server/routers.py` 的真实执行行」。`pytestmark = [pytest.mark.mes, pytest.mark.regression]`（**没有 api 标记**，所以 `-m api` 组仍是 116 条）。 |
| `.coveragerc` | 14 | 覆盖率配置的唯一载荷点，只写 `[run] parallel = true`。为什么不能塞进 `pytest.ini` 的 addopts 见 §3。 |

### 修改

| 文件 | 改动 | 为什么 |
|---|---|---|
| `testcases/mes/mock_server_ctl.py` | `+64 / -7`（见 `git diff`） | 3 处：① `start()` 的 cmd 由 `python -m mock_server.run` 改成 `python scripts/service_launcher.py mock_server.run`；② 新增模块级 `SERVICE_LAUNCHER` 常量与 `subprocess_coverage_env(data_file, source=None)` 辅助函数；③ `stop()` 由「一句 `terminate()`」改成**先礼后兵**（优雅信号 → `terminate()` → `kill()`），并新增 `_signal_graceful()`。 |
| `testcases/login/login_server_ctl.py` | `+47 / -8` | 同一套改动（launcher + 先礼后兵），另外 `__init__` 新增 `extra_env: dict[str, str] \| None = None` 参数——原来只有 `MockServer` 有，登录服务的覆盖率要能单独指向临时 data file 就得对齐。向后兼容（有默认值）。 |

### 提交记录

见 §11。

---

## 2. 机制：为什么之前是 0%，现在为什么不是

### 2.1 数据本来就采得到，丢在**退出路径**上

`pytest-cov.pth`（随 pytest-cov 装在 site-packages）在每个解释器启动时读 `COV_CORE_SOURCE`，命中就
`pytest_cov.embed.init()` 起一个 `coverage.Coverage(data_suffix=True, auto_data=True, data_file=COV_CORE_DATAFILE)`。
`mock_server_ctl.py` 的 `env = {**os.environ, ...}` 本来就把这些变量透传给子进程，所以**服务进程里的行数据一直在被记录**。

问题是落盘：`coverage` 的数据写在 `atexit` 里（`coverage/control.py:654 atexit.register(self._atexit)`，`:745 def _atexit`）。
而 `stop()` 原来那一句 `self.proc.terminate()` 在 Windows 上是 `TerminateProcess` —— **不跑 atexit**，进程里攒了一整轮的行数据随进程一起消失。

实测（`-m mes --cov=mock_server`，改造前）：

```
mock_server\routers.py         198    198     0%
mock_server\services.py        359    359     0%
mock_server\deps.py             67     67     0%
TOTAL                         1374   1245     9%
```

`9%` 全部来自 import 期的模块级语句；Handler 路径一行都没有。

### 2.2 为什么「直接发优雅信号」也不行

`uvicorn` 0.34.0 自己处理 `SIGINT/SIGTERM`，Windows 上还处理 `SIGBREAK`（`uvicorn/server.py:29-35`
`HANDLED_SIGNALS += (signal.SIGBREAK,)`）。它的 `capture_signals()`（`server.py:312-330`）在 `finally` 里
**把原 handler 装回去、然后 `signal.raise_signal(原信号)` 把信号重发给自己**：

```python
finally:
    for sig, handler in original_handlers.items():
        signal.signal(sig, handler)
for captured_signal in reversed(self._captured_signals):
    signal.raise_signal(captured_signal)
```

重发之后是**默认处置** → 进程被终止 → `atexit` 依旧不执行。

实测三组对照（同一支 `python -m mock_server.run` + 同一套 `COV_CORE_*`，`work_tmp/probe10.py`）：

| 退出方式 | returncode | 分片里有真实行的文件数 |
|---|---|---|
| A `terminate()` | 1 | 0（全部 14 个模块命中行数均为 0） |
| B 直发 `CTRL_BREAK_EVENT` 给服务进程 | 3 | 0 |
| C 经 `service_launcher.py`（进程内吸附重发信号）+ `CTRL_BREAK_EVENT` | **0** | **14**（`routers.py` 257 行、`services.py` 103、`deps.py` 46 …） |

### 2.3 最终方案：进程内「吸收重发信号」

`scripts/service_launcher.py` 在 `runpy.run_module()` **之前**把 `SIGINT/SIGTERM/SIGBREAK` 换成只记录不终止的 handler。
uvicorn 重发信号时被它接住（进程不退出），`serve()` 正常返回 → `main()` 返回 → `sys.exit(0)` → **`atexit` 执行 → 分片落盘**。
服务日志里能看到这条路径确实走到了（原始输出）：

```
[mock_server] 监听 http://127.0.0.1:xxxx/docs
[launcher] absorbed re-raised signal 21
[launcher] SystemExit 0
[launcher] target returned
```

### 2.4 被否决的路（以及否决依据）

| 候选 | 否决理由（实测/源码） |
|---|---|
| 保持 `terminate()` | 就是病根：`TerminateProcess` 不跑 atexit。 |
| 只发 `CTRL_BREAK_EVENT`（等 uvicorn 自己关） | 实测 returncode=3、分片仍为 0 行——uvicorn 把信号重发回来打死了自己（§2.2）。 |
| 用 `coverage run --parallel-mode` 把服务包一层 | 解决不了退出问题（包一层的进程同样被 `TerminateProcess` 杀掉）；而且会与 pytest-cov 的 `COV_CORE_*` 通道双开、重复测量同一份代码。 |
| 走 `COVERAGE_PROCESS_START` + `a1_coverage.pth` 通道 | 该 `.pth` 在**本 venv 直接抛异常**（`Error processing line 1 of ...\site-packages\a1_coverage.pth` + `Traceback ... File "<frozen site>", line 206, in addpackage`），实测什么都没写。本方案走的是 `pytest-cov.pth` 那条通道。 |
| 给服务加「周期性 flush」 | 要改 `mock_server/**`（被测对象），越界且把覆盖率需求渗进生产代码。 |
| 改 `mock_server/run.py` 自己装 handler | 同上：覆盖率不该要求被测对象改代码。所以做成独立脚本。 |
| 把 `--cov*` 塞进 `pytest.ini` 的 addopts | 见 §3。 |

---

## 3. 配置载体：为什么是 `.coveragerc` 而不是 `pytest.ini` 的 addopts

`.coveragerc` 全文只有一句有效配置：

```ini
[run]
parallel = true
```

放在这里的理由（写进了文件的注释里）：

1. **这份配置的读者不止 pytest。** 被测服务跑在子进程里，那里的 coverage（`COV_CORE_*` 通道、`COVERAGE_PROCESS_START` 通道、以及人手工敲的 `coverage combine/report`）都读这个文件，而 **coverage 根本不解析 `pytest.ini`** —— 塞在 `pytest.ini` 里的配置对子进程一律无效。
2. **addopts 是「跑 pytest 这一次」的命令行参数**，只能注入给 pytest 自己；而覆盖率要采集的恰恰是 pytest 之外的那些进程。
3. **`[run]` 段的开关没有对应的命令行选项**（`parallel`、`sigterm` 都是），只能落配置文件。

pytest-cov 无需任何额外参数就会读它：`pytest_cov/plugin.py:114-117`
`group.addoption('--cov-config', ..., default='.coveragerc')` 默认值就是仓库根的 `.coveragerc`。

**载体选型补充（必须如实说明的一点）**：实测 A/B（有 `.coveragerc` vs 没有，跑同一条 `-m mes --cov=mock_server`）
**数字完全相同**（都是 `TOTAL 1374 235 83%`，`routers 84% / services 70% / deps 82%`）。
原因是 `pytest_cov.embed.init()` 自己就传了 `data_suffix=True`，不依赖 `parallel`。
所以 `.coveragerc` **不是子进程采集能否落盘的必要条件**；它的真实作用是：
① 给「手工 combine/report」与「未来可能启用的 `COVERAGE_PROCESS_START` 通道」一个确定性载荷点；
② 把「分片模式」这个约定写进版本库，而不是靠人记住命令行参数；
③ 只在这一个地方写清「为什么不用 pytest.ini」。

`parallel = true` 具体影响哪条通道（实测，独立目录里 `python -c "import mock_server.routers, mock_server.services"`）：

| 通道 | `parallel=true` | `parallel=false` |
|---|---|---|
| `COVERAGE_PROCESS_START` | 写分片（1 个），无基础 `.coverage` | 写进基础 `.coverage`（无后缀） |
| `COV_CORE_*`（pytest-cov） | 2 个分片 | 2 个分片（自带 `data_suffix=True`，不受影响） |

另：**不要**在 `.coveragerc` 里写 `fail_under`。`pytest_cov/plugin.py:263-267` 会在 `--cov-fail-under` 缺省时取配置文件里的值，写进去等于偷偷改了 CI 门禁。

---

## 4. 分片怎么合并：pytest-cov 会自动 combine（实测，不是猜）

**结论：会自动 combine。** 走 `pytest --cov=...` 时不需要任何手工步骤。

源码依据：`pytest_cov/engine.py` 的 `Central.finish()`（session 结束）：

```python
self.unset_env()
self.cov.stop()
self.cov.save()
self.cov = self.combining_cov
self.cov.load()
self.cov.combine()
self.cov.save()
```

`-n 4 --dist loadfile` 时由 `DistMaster.finish()` 做同一串（`engine.py:371-381`）；
各 worker（`DistWorker.finish()`，`:415-429`）只 `stop()/save()`，不 combine——源码注释写明是为了避免 `.combine()` 的竞态。

实测（`work_tmp/final6/`，跑完 `-m api -n 4 --cov=. --cov-report=term` 后）：

```
跑完根目录 .coverage.* 分片现存数量 = 0
根目录只剩一个 .coverage（53248 字节）
```

`coverage/control.py: def combine(self, data_paths=None, strict=False, keep=False)` 默认 `keep=False`，
所以分片被合并后**会被删掉**，不往工作区留垃圾。

另有一条容易踩的语义（实测）：pytest-cov 在 `--cov-append` **缺省**时会先 `erase()`，
而 `coverage/sqldata.py: def erase(self, parallel=False)` 在 `parallel=True` 时会
`glob.escape(local_abs_path) + ".*"` **把会话开始前就存在的分片一起删掉**。
实测：预置一个假分片后跑 `-m login --cov=...` → 报告里那份文件是 `0%` 且伴随
`CoverageWarning: No data was collected.`；加上 `--cov-append` 后同一实验变成 `25%`。
**含义**：分片必须在 pytest **会话期间**产生（本方案正是如此：服务在用例执行中被拉起），
不能靠「先跑一次再把分片留给下一次」。

### 本地可复现的单条命令（不需要 `--cov` 的那条路）

元测试自己注入 `COV_CORE_*` 并把 data file 指到自己的 `tmp_path`，所以可以脱离 `--cov` 单独复现：

```powershell
& $v -m pytest testcases/mes/test_coverage_collection.py -q      # 分片落在它自己的 tmp 目录
Set-Location $shardDir; & $v -m coverage combine; & $v -m coverage report -m
```

实测（`work_tmp/final6/manual_combine.txt`，`$shardDir` = `...\pytest-of-lefan\pytest-NNN\test_service_subprocess_shard_0`）：

```
.coverage.07M.pid40416.XW1Wq5xx.HBJ78DRAKxQh
.coverage.07M.pid40416.XYgYYtlx.HNQIp837rWOh
$ coverage combine          → Combined 2 files      （exit=0）
$ coverage report -m        → TOTAL 3028 2262 25%   （exit=0）
  mock_server\routers.py 198 126 36% / services.py 359 309 14%
```

两条踩过的坑（已写进元测试的注释）：
- 列分片必须用 `.coverage.*`，**不能用 `.coverage*`** —— 后者会把 `.coveragerc` 也匹配进来，再当 sqlite 读就报
  `coverage.exceptions.DataError: Couldn't use data file '...\.coveragerc': file is not a database`。
- 不要把路径当参数传给 `coverage combine`（`coverage combine <某路径>` 会被当成位置参数式的 rc 名而失败，
  报 `No data collected: file doesn't exist`）。**cd 进分片目录后裸跑**才对。

---

## 5. 元测试与反向对照

### 5.1 元测试断言了什么

`testcases/mes/test_coverage_collection.py`（85 行）自己起服务、打 3 次 `/api/v1/healthz`、`stop()`，然后：

1. `assert new_shards`：`tmp_path` 里**必须多出 `.coverage.*` 分片**；
2. `assert any(count > 0 ...)`：分片里 `mock_server/routers.py` **必须有真实执行的行**。

第 2 条是必要的：Windows 上「硬杀」恰好会留下一个**空壳分片**（`pytest_cov.embed.init()` 被 site 处理两次，
第二次 `cleanup()` 会用零数据落一次盘），「文件在、数据空」正是这个 bug 最迷惑人的形态。

### 5.2 反向对照（证明元测试在机制被破坏时真的变红）

**变体 A —— 去掉覆盖率注入**（删掉 `extra_env=subprocess_coverage_env(data_file),` 这一行）：

```
testcases\mes\test_coverage_collection.py:73: AssertionError
E       AssertionError: 服务子进程没有写出任何覆盖率分片：子进程根本没拿到覆盖率埋点。临时目录内容=['coverage-server.log', 'coverage.db']，...
E       assert set()
======================== 1 failed, 1 warning in 4.40s =========================
FAILED testcases/mes/test_coverage_collection.py::test_service_subprocess_shard_contains_real_lines
```

**变体 B —— 保留注入、把 `stop()` 退回硬杀**（`self._signal_graceful()` → `self.proc.terminate()`，即原 bug 的形态）：

```
testcases\mes\test_coverage_collection.py:80: AssertionError
E       AssertionError: 分片文件存在但 mock_server/routers.py 一行执行记录都没有 —— 这就是覆盖率盲区的原始症状。最常见原因是服务进程被硬杀（Windows 上 terminate() = TerminateProcess，不跑 atexit），覆盖率数据没机会落盘，只留下 embed 二次 init 写的空壳。各分片命中的 routers.py 行数={'.coverage.07M.pid38940.XBM9vPex.HNQIp837rWOh': 0}，...
E       assert False
======================== 1 failed, 1 warning in 5.06s =========================
FAILED testcases/mes/test_coverage_collection.py::test_service_subprocess_shard_contains_real_lines
```

**还原后**：`1 passed, 1 warning in 5.06s`（exit=0），且两个被破坏的文件 `git hash-object` 与基准逐字节一致：

```
76F2B768B571C67EA098A9B36650920D29A7C9369836695701B6CC2461088C66  testcases/mes/mock_server_ctl.py
92BBBDDDCA13060AE33EC248425904B4C932BFEE6CF84C4DCC6C4D6BDD0457E4  testcases/mes/test_coverage_collection.py
```

（中途出现过两次**无效对照**：一次是 `.coveragerc` 被我自己误删、一次是替换后命令变成了
`python, "mock_server.run"`（丢了 `-m`）导致服务根本起不来，报
`MockServerError: Mock 服务进程已退出（returncode=2）。启动日志: ... can't open file '...\mock_server.run'`。
两次都作废重做——这里保留是为了说明「对照必须核对替换后的真实文本」。）

---

## 6. 覆盖率改前 / 改后对照

「改前」= 工作区回到 `44fc60d`（两个 ctl 已 `git checkout`，3 个新文件移走）；
「改后」= 提交态字节。两组都在同一台机器上、同一天、同一条命令跑出来，原始文件在 `work_tmp/final6/`。

### 6.1 `-m mes --cov=mock_server`（`before_09_mes_cov.txt` → `after_09_mes_cov.txt`）

| 模块 | 改前 Stmts/Miss/Cover | 改后 Stmts/Miss/Cover |
|---|---|---|
| `mock_server/__init__.py` | 12 / 8 / 33% | 12 / 6 / 50% |
| `mock_server/app.py` | 40 / 40 / **0%** | 40 / 4 / **90%** |
| `mock_server/db.py` | 116 / 85 / 27% | 116 / 18 / 84% |
| `mock_server/deps.py` | 67 / 67 / **0%** | 67 / 12 / **82%** |
| `mock_server/errors.py` | 97 / 3 / 97% | 97 / 0 / 100% |
| `mock_server/idempotency.py` | 70 / 70 / **0%** | 70 / 6 / **91%** |
| `mock_server/models.py` | 27 / 27 / **0%** | 27 / 1 / **96%** |
| `mock_server/quantity.py` | 82 / 82 / **0%** | 82 / 33 / **60%** |
| `mock_server/routers.py` | 198 / 198 / **0%** | 198 / 32 / **84%** |
| `mock_server/run.py` | 37 / 37 / **0%** | 37 / 4 / **89%** |
| `mock_server/schemas.py` | 186 / 186 / **0%** | 186 / 0 / **100%** |
| `mock_server/security.py` | 61 / 61 / **0%** | 61 / 8 / **87%** |
| `mock_server/services.py` | 359 / 359 / **0%** | 359 / 109 / **70%** |
| `mock_server/util.py` | 22 / 22 / **0%** | 22 / 2 / **91%** |
| **TOTAL** | **1374 / 1245 / 9%** | **1374 / 235 / 83%** |

用例数：`54 passed, 300 deselected` → `55 passed, 300 deselected`（多出来的正是新增元测试）。

### 6.2 `-m login --cov=login_app`（`before_10_login_cov.txt` → `after_10_login_cov.txt`）

| 模块 | 改前 | 改后 |
|---|---|---|
| `login_app/__init__.py` | 0 / 0 / 100% | 0 / 0 / 100% |
| `login_app/app.py` | 69 / 42 / 39% | 69 / 13 / **81%** |
| `login_app/run.py` | 19 / 19 / **0%** | 19 / 0 / **100%** |
| **TOTAL** | **88 / 61 / 31%** | **88 / 13 / 85%** |

### 6.3 全量 `-m "not ui" -n 4 --dist loadfile --cov=. --cov-report=term --cov-fail-under=40`

| | 改前（`before_08_full_cov.txt`） | 改后（`after_08_full_cov.txt`） |
|---|---|---|
| 用例 | `329 passed, 11 xfailed` | `330 passed, 11 xfailed` |
| **TOTAL** | **3953 / 1016 / 74%**（`74.30%`） | **4055 / 450 / 89%**（`88.90%`） |
| `mock_server/routers.py` | 198 / 128 / 35% | 198 / 6 / 97% |
| `mock_server/services.py` | 359 / 309 / 14% | 359 / 77 / 79% |
| `mock_server/deps.py` | 67 / 28 / 58% | 67 / 4 / 94% |
| `mock_server/app.py` | 40 / 13 / 68% | 40 / 1 / 98% |
| `mock_server/db.py` | 116 / 33 / 72% | 116 / 13 / 89% |
| `mock_server/run.py` | 37 / 37 / 0% | 37 / 4 / 89% |
| `login_app/app.py` | 69 / 42 / 39% | 69 / 11 / 84% |
| `login_app/run.py` | 19 / 19 / 0% | 19 / 0 / 100% |
| 门禁 | `Required test coverage of 40% reached. Total coverage: 74.30%`，exit=0 | `... 88.90%`，exit=0 |

`Stmts` 从 3953 涨到 4055：新增的 `scripts/service_launcher.py 37` + `testcases/mes/test_coverage_collection.py 34`
+ 两个 ctl 的净增行，都是新代码。

### 6.4 CI 那一组 `-m api -n 4 --dist loadfile --cov=. --cov-report=term --cov-report= --cov-fail-under=40`

| | 改前 | 改后 |
|---|---|---|
| 用例 | `116 passed, 4 warnings` | `116 passed, 4 warnings` |
| **TOTAL** | **3883 / 1855 / 52%**（`52.23%`） | **3985 / 842 / 79%**（`78.87%`） |
| `mock_server/routers.py` | **0%** | 84% |
| `mock_server/services.py` | **0%** | 70% |
| `mock_server/deps.py` | **0%** | 82% |
| `login_app/app.py` | 39% | 81% |

改前原始输出：`work_tmp/final3/13_api_cov_BEFORE.txt`
（`TOTAL 3883 1855 52%` / `Required test coverage of 40% reached. Total coverage: 52.23%`）。
改后：`work_tmp/final6/after_12_api_cov.txt`。

---

## 7. 自检命令与原始输出

全部在 `workdir = C:\Users\lefan\DSH_project\接口自动化_3\work\w2-coverage`、`$v = <venv>\Scripts\python.exe` 下跑，
每条命令前先删 `.coverage`。原始输出落盘 `work_tmp/final6/`，`summary.txt` 汇总 exit 与墙钟秒数。

| # | 命令 | exit | 墙钟 | 结果原文（尾行） |
|---|---|---|---|---|
| ① | `& $v -m ruff check .` | 0 | 0.2 | `All checks passed!` |
| ② | `& $v -m ruff format --check .` | 0 | 0.2 | `83 files already formatted` |
| ③ | `& $v -m mypy`（**无参数**，走 `pyproject.toml` 的 `files`） | 0 | 0.8 | `Success: no issues found in 67 source files` |
| ④ | `& $v -m pytest -m api -n 4 --dist loadfile -q` | 0 | 28.3 | `116 passed, 4 warnings in 25.53s` |
| ⑤ | `& $v -m pytest -m mes -q` | 0 | 32.8 | `55 passed, 300 deselected, 1 warning in 30.15s` |
| ⑥ | `& $v -m pytest -m login -q` | 0 | 4.7 | `29 passed, 326 deselected, 1 warning in 2.49s` |
| ⑦ | `& $v -m pytest -m contract -q` | 0 | 25.1 | `213 passed, 131 deselected, 11 xfailed, 4 warnings in 22.27s` |
| ⑧ | `& $v -m pytest -m "not ui" -n 4 --dist loadfile --cov=. --cov-report=term --cov-fail-under=40 -q` | 0 | 52.9 | `330 passed, 11 xfailed, 7 warnings in 49.47s` + `TOTAL 4055 450 89%` + `Required test coverage of 40% reached. Total coverage: 88.90%` |
| ⑨ | `& $v -m pytest -m mes --cov=mock_server --cov-report=term -q` | 0 | 45.0 | `55 passed, 300 deselected, 1 warning` + `TOTAL 1374 235 83%` |
| ⑩ | `& $v -m pytest -m login --cov=login_app --cov-report=term -q` | 0 | 9.5 | `29 passed, 326 deselected, 1 warning` + `TOTAL 88 13 85%` |
| ⑪ | `& $v -m pytest testcases/mes/test_coverage_collection.py -q`（元测试单跑，**不带 `--cov`**） | 0 | 9.0 | `1 passed, 1 warning in 6.36s` |
| ⑫ | `& $v -m pytest -m api -n 4 --dist loadfile --cov=. --cov-report=term --cov-report= --cov-fail-under=40 -q` | 0 | 32.0 | `116 passed, 4 warnings in 28.77s` + `TOTAL 3985 842 79%` |

「改前」同一批命令（工作区回到 `44fc60d`）也全部 exit=0，原始输出在 `work_tmp/final6/before_*.txt`
（`before_05_mes` = `54 passed, 300 deselected, 1 warning in 23.91s`；`before_08_full_cov` = `329 passed, 11 xfailed, 7 warnings in 43.43s` + `TOTAL 3953 1016 74%`）。

补充：一次修复前的 `ruff check .` 曾红过一次，全部错误来自我自己放在 `work_tmp/` 里的临时探针
（`work_tmp/probe11.py:33:1: W605 Invalid escape sequence: \S` 等），删掉 `work_tmp/**/*.py` 后转绿——
**基线 `44fc60d` 的 lint 本来就是绿的**（实测 `00_base_ruff_check.txt` = `All checks passed!`、`00_base_ruff_format.txt` = `84 files already formatted`）。

---

## 8. 耗时代价

**结论：+1 秒量级（约 +4%），不是数量级变化。**

- **受控隔离测量**（把新增元测试排除，前后各 54 条用例，同一条 `-m mes --cov=mock_server`）：
  `26.18s → 27.30s`，**+1.12s / +4.3%**（`work_tmp/final6/isolate/`）。
- 机理：每个服务实例的 `stop()` 多一次优雅信号往返，单次实测 wait ≈ **0.32–0.34s**；
  一个 mes 会话只有 3 次左右 `stop()`（session 服务 + `test_auth_secrets` + `test_token_lifecycle` 两个短命服务）。
- **`final6/summary.txt` 里的整轮墙钟数字不要当结论用**：同一命令 `08_full_cov` 在 final4 是 35.85s、final6 是 52.9s，
  `09_mes_cov` 在 final4 是 30.96s、final6 是 45.0s——本机负载抖动远大于本次改动的增量。
  上表只保证「每条都 exit=0」，耗时以隔离测量为准。

---

## 9. 给调度者的整合清单

### 9.1 `.github/workflows/test.yml` —— **门禁命令不需要改**

`pytest -m api -n 4 --dist loadfile --cov=. --cov-report=term --cov-report= --cov-fail-under=40 --alluredir=allure-results`
（`test.yml:245-249`）原样就能吃到本次改动：`--cov-config` 默认就是 `.coveragerc`（`pytest_cov/plugin.py:117`），
子进程通过 `COV_CORE_*` 自动接管，`Central.finish()` 自动 combine。**不需要加 `coverage run --parallel-mode`，也不需要加手工 combine 步骤。**

### 9.2 `test.yml:263` 与 `:278-292` 的注释 —— **必须改写（原文已不成立）**

这三处直接点名本次任务，且写的是旧结论。建议把 `:276-289` 那一段整体替换为：

```yaml
      # ⚠️ 关于 `mock_server/*` 的覆盖率（波 2 · P1-3 已修，见 docs/wave2-coverage-handoff.md）：
      #
      #   1. 由 fixture 以子进程拉起的那个真 Mock 服务**已经被统计**。做法：
      #      testcases/mes/mock_server_ctl.py 经 scripts/service_launcher.py 启动服务，
      #      并在 stop() 里先发优雅信号（Windows: CTRL_BREAK_EVENT）再 terminate，
      #      让服务进程能正常返回到 atexit —— coverage 的数据是在 atexit 落盘的。
      #      launcher 的作用是吸收 uvicorn 关闭时"重发信号"的默认终止动作。
      #   2. 不再需要 `-m coverage run --parallel-mode`：pytest-cov 的 COV_CORE_*
      #      通道本来就覆盖了子进程，分片由 pytest-cov 在 session 结束时 combine。
      #   3. 本组实测（本机）由 52.23%（3883/1855）升到 **78.87%**（3985/842）；
      #      mock_server/routers.py 0% → 84%，services.py 0% → 70%，deps.py 0% → 82%。
      #      全量 `-m "not ui"` 由 74.30% 升到 88.90%。
```

另建议把 `:263` 那句 `# 注意：同一份代码全量跑（-m "not ui" --cov=.）是 **74.30%**` 改为 `**88.90%**`。

### 9.3 `test.yml:399-413` 的「覆盖率写进流水线摘要」步骤 —— **建议改写尾注**

`:410-412` 那两行 echo：

```yaml
            echo "> \`mock_server/*\` 的数字只覆盖「模块级执行」（导出脚本子进程被 pytest-cov 记账），" >> "$GITHUB_STEP_SUMMARY"
            echo "> 由 fixture 拉起的真服务子进程仍未被统计，\`services.py\`（359 条）是当前最大盲区。" >> "$GITHUB_STEP_SUMMARY"
```

建议替换为：

```yaml
            echo "> \`mock_server/*\` 的数字**已经包含**由 fixture 拉起的真服务子进程（波 2 · P1-3，见 docs/wave2-coverage-handoff.md）。" >> "$GITHUB_STEP_SUMMARY"
            echo "> 服务经 scripts/service_launcher.py 启动并优雅退出，coverage 分片由 pytest-cov 自动 combine。" >> "$GITHUB_STEP_SUMMARY"
```

### 9.4 CI 覆盖率门禁数字 —— **建议不调，只更新注释里的实测值**

门禁仍是 `--cov-fail-under=40`。理由与现有注释一致：门禁拦的是**退化**，不是卡住当前值；
而这一组的实测值从 52.23% 跳到 78.87%（本机），其中一部分来自公网接口用例在 runner 上的抖动。
建议保持 40，把 `:263` 与 `:276` 附近注释里的「52.23% / 74.30%」更新为「78.87% / 88.90%」。
**若**整合者想让门禁更有牙，可以后续单独评估提到 70——但那需要在 CI 上先真跑一次拿到 runner 实测值，本机数据不足以支撑。

### 9.5 `README.md` —— **必须改（否则文档与代码不符）**

`:399-400` 两行数字：

```markdown
pytest -m "not ui" -n 4 --cov=. --cov-report=term          TOTAL 3953 1016 74%（74.30%）
pytest -m api -n 4 ... --cov-fail-under=40                 TOTAL 3883 1855 52%（52.23%）
```

建议改为：

```markdown
pytest -m "not ui" -n 4 --cov=. --cov-report=term          TOTAL 4055 450 89%（88.90%）
pytest -m api -n 4 ... --cov-fail-under=40                 TOTAL 3985 842 79%（78.87%）
```

`:417-443` 整段（标题是「**`mock_server/*` 不再恒为 0%，但盲区没有被消掉**」）**建议整段重写**，原文的结论（「收尾走 `terminate()`，而 Windows 上是 `TerminateProcess` 强杀」「`services.py`（359 条 statements）仍然是完整的盲区——这是下一波要盯的地方」）已被本次改动推翻。建议原文：

```markdown
> **`mock_server/*` 的覆盖率现在是真行为覆盖**（波 2 · P1-3，见 `docs/wave2-coverage-handoff.md`）。
>
> 之前那 0%/14% 是**采集失败**，不是"没人跑过"：被测服务由 fixture 以常驻子进程拉起
> （`testcases/mes/mock_server_ctl.py`），收尾走 `terminate()`，而 Windows 上是
> `TerminateProcess` 强杀 —— coverage 靠 `atexit` 落盘，被强杀就什么都不剩。
>
> 现在服务经 `scripts/service_launcher.py` 启动：launcher 先在进程内装一个"吸收信号"的 handler，
> uvicorn 关闭时会 `signal.raise_signal(原信号)` 把信号重发给自己（默认处置 = 不跑 atexit），
> 这个 handler 把它接住，于是 `serve()` 正常返回、进程走到 `atexit`、分片落盘；
> `stop()` 也改成先礼后兵（优雅信号 → `terminate()` → `kill()`）。
> 分片由 pytest-cov 在 session 结束时自动 combine，不需要手工 `coverage combine`。
>
> 实测（本机）：`-m mes --cov=mock_server` → `TOTAL 1374 235 83%`
> （`routers.py` 0% → 84%、`services.py` 0% → 70%、`deps.py` 0% → 82%）。
> `login_app` 同构：`-m login --cov=login_app` → `TOTAL 88 13 85%`（`run.py` 0% → 100%）。
```

### 9.6 `CHANGELOG.md` —— **必须删掉「尚未修复的已知问题」第 1 条**

`:251-257` 第 1 条（`- **mock_server/* 覆盖率恒为 0%**（1358 条 statements）。需要给子进程加 -m coverage run --parallel-mode 并在用例结束后 coverage combine。本机未做。这也是覆盖率门禁只能定在 40% 的根本原因。`）
**建议整条删除**（它已经不是「尚未修复」），并在 `## [未发布]` 下新增一节：

```markdown
### 新增 —— 子进程服务的覆盖率采集（波 2）

被测服务由 fixture 以常驻子进程拉起，收尾走 `terminate()`；Windows 上那是
`TerminateProcess`，不跑 `atexit`，而 coverage 的数据正是在 `atexit` 落盘——
所以 `mock_server/*` 的覆盖率恒为 0%（Handler 路径一行都没记上）。

修法不是给子进程套 `coverage run --parallel-mode`，而是给它一条**能正常返回**的退出路径：
新增 `scripts/service_launcher.py`，在进程内装一个"吸收信号"的 handler 后再
`runpy.run_module(<服务模块>)`。uvicorn 关闭时会 `signal.raise_signal(原信号)` 把信号
重发给自己（默认处置终结进程、不跑 atexit），launcher 把它接住，于是 `serve()` 正常返回、
`atexit` 执行、分片落盘；`stop()` 同时改成先礼后兵（优雅信号 → `terminate()` → `kill()`）。
新增 `.coveragerc`（`[run] parallel = true`）承载分片模式，分片由 pytest-cov 在 session
结束时自动 combine。

实测：`-m mes --cov=mock_server` 的 TOTAL 由 9%（1245 miss）升到 83%（235 miss），
`routers.py` 0% → 84%、`services.py` 0% → 70%、`deps.py` 0% → 82%；
全量 `-m "not ui"` 由 74.30% 升到 88.90%；CI 那一组 `-m api` 由 52.23% 升到 78.87%。
新增元测试 `testcases/mes/test_coverage_collection.py` 钉住机制（去掉注入或把 `stop()`
退回硬杀，它都会红）。详见 `docs/wave2-coverage-handoff.md`。
```

### 9.7 `requirements.txt` / `requirements.lock` —— **不需要新增依赖**

本方案只用已装的东西：`pytest-cov==6.0.0`（`requirements.txt:7`）+ 它传递引入的 `coverage`（7.16.2，**没有被直接 pin**）。
CI 里已有 `python -m coverage report ...` 那一步（`test.yml:407`），依赖`coverage` 一定在。

### 9.8 `pytest.ini` —— **不需要改**

没有新增 marker，也没有动 addopts。新增元测试复用已有的 `mes` 与 `regression`（`pytest.ini:13-19` 已声明）。

### 9.9 `.gitignore` —— **建议加一条（本文件作者边界外，未自行修改）**

`.gitignore:14` 只有 `.coverage`，**没有 `.coverage.*`**（实测 `git check-ignore -v ".coverage.07M.pid1.X.Y"` → exit=1）。
正常跑完 `combine` 会把分片删掉（实测跑完残留 0 个），所以不作为不会立刻显形；
但**中断、`--collect-only`、或 `pytest -x` 提前退出**时会留下 `.coverage.主机.pid.XXX` 分片。
建议在第 14 行补一行：

```
.coverage
.coverage.*
```

（同理，`work_tmp/` 是我自己的临时目录，不会入库，不需要加规则。）

---

## 10. 未做到 / 不确定的地方

1. **Linux/CI 上的行为完全没有验证过。** 本机是 Windows 11。方案在 POSIX 上走 `SIGTERM`
   （`_signal_graceful` 里 `os.name != "nt"` 分支），而 uvicorn 的 `capture_signals()` 同样会把它重发回来、
   launcher 也装了 `SIGTERM` 的吸附 handler，**理论上**同构成立——但这是推理，不是实测。
   CI 是 `ubuntu-latest`，第一次跑请重点看 `-m api --cov=.` 的 TOTAL 有没有落到 70%+ 这一带。
2. **`-n 4 --dist loadfile` 下的合并**验过：`final6/after_12_api_cov.txt`（CI 同款命令）与
   `after_08_full_cov.txt` 都是 `-n 4`，数字正常、跑完残留分片 0 个。但**没有**专门构造过
   「多个 worker 同时写同一目录分片」的压力场景去验证竞态；依据是分片名带 pid+随机后缀、互不覆盖。
3. **`.coveragerc` 不是必要条件**（§3 的 A/B 实测）。如果整合者觉得多一个配置文件不值，删掉它
   生产通道仍然工作；但那样「手工 combine/report」与 `COVERAGE_PROCESS_START` 通道就失去了
   确定性载荷点。
4. **每次 `start()` 会多一个零数据空壳分片**（`pytest_cov.embed.init()` 被 site 处理两次，第二次
   `cleanup()` 用零数据落一次盘）。它对最终数字无影响（combine 后行数据是并集），但 `coverage debug data`
   里会看到它。没有去消除它——消除需要动 `pytest_cov.embed`，不值得。
5. **`_signal_graceful()` 用 `getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)`**：`login_server_ctl.py`
   里写成裸的 `signal.CTRL_BREAK_EVENT`（同样有 `os.name == "nt"` 守卫，mypy 与 ruff 都过）。
   两个文件风格不一致，但都只是「非 Windows 平台上该属性不存在」这一点的两种写法，无行为差异。
6. **`testcases/mes/test_coverage_collection.py` 依赖 `server._read_log_tail()`（私有方法）**——
   仅用于**失败时的诊断信息**，不影响断言。如果以后重命名它，元测试会报 AttributeError 而不是给出诊断。
7. **端口 flake 仍存在，且不是本次引入的**：`free_port()` 是「绑定一次拿到端口号、关掉、再交给服务」，
   中间有竞态窗口。观察到一个既有 flake：`test_auth_secrets.py::test_custom_admin_password_changes_both_seed_and_client`
   失败于 `MockServerError: ... returncode=1`，服务日志尾部为
   `ERROR: [Errno 13] error while attempting to bind on address ('127.0.0.1', 2963): [winerror 10013]`。
   本机 `netsh int ipv4 show excludedportrange protocol=tcp` 显示管理员排除段只有 `50000–50059`，
   而 `netsh int ipv4 show dynamicport tcp` 的范围是 **1024–15000**——即故障端口落在 Windows 保留端口段里。
   建议在**别的一波**给 `free_port()` 加「启动失败就换一个端口重试」，本次没做（超出本任务边界）。
8. **`extra_env` 是 `LoginServer` 新增的公开参数**（`MockServer` 早就有）。向后兼容，但确实扩大了 `__init__` 签名。
9. **macOS / Python 3.10、3.11 未验证**（`pyproject.toml` 声明 `requires-python = ">=3.10"`）。
   `signal.SIGBREAK` 在非 Windows 上不存在，代码用 `getattr(..., None)` 跳过；POSIX 路径见第 1 条。
10. **没有动 `pyproject.toml`**：配置载体选了 `.coveragerc`（pytest-cov 的默认 `--cov-config` 值就是它），
    所以不需要 `[tool.coverage]` 段。两者同时存在会让人不知道该看哪个。

---

## 11. 提交记录

见 `git log --oneline` 与 `git diff --stat 44fc60d HEAD`（在本文件的第 1 节表格里给了逐文件改动；
提交本身由整合者核对）。提交信息过 `scripts/check_commit_msg.py`。

---

## 12. 撤回与复现用到的临时产物（不入库）

`work_tmp/`（7.6 MB / 404 项）**已在提交前整体删除，不入库**——它是本波的探针与原始输出目录，
留在工作副本里会被 `ruff check .` 扫到（见 §7 的 lint 陷阱）。下表记录它曾经存有什么，
以便整合者判断「本文件里的数字还能怎么复核」：

| 曾经路径 | 内容 | 现在怎么复核 |
|---|---|---|
| `work_tmp/final6/` | 权威验收：`before_*.txt` / `after_*.txt` 各 12 条命令的原始输出 + `summary.txt`（exit/秒数）+ `isolate/`、`interleave/`（耗时测量） | 本文件 §6/§7/§8 已逐行摘录原始输出行；重跑 §7 的命令即可复现 |
| `work_tmp/rev4/` | 反向对照 A/B/C 的原始输出（A、B 红；C 还原后绿） | 本文件 §5.2 已摘录断言与首尾行；按 §5.2 的两处单行改动复现 |
| `work_tmp/keep/`、`work_tmp/revctrl/` | 提交态 5 个文件的逐字节备份（用于反向对照后证明还原一致） | 不再需要：文件已入库，直接 `git diff HEAD -- <path>` 为空即是提交态 |
| `work_tmp/final3/` | 早期验收 + `13_api_cov_BEFORE.txt`（CI 组改前真基线 `TOTAL 3883 1855 52%`） | 本文件 §6.4 已摘录该行 |

`work_tmp/` 在 `.gitignore` 里**没有**规则（它靠「不被 `git add`」而不入库）。如果你要恢复这套取证流程，
用自己的临时目录即可；`work_tmp/` 下**不要留 `.py`**，否则 `ruff check .` / `ruff format --check .` 会被它带红
（实测 `ruff format --check` 甚至会在这种输入上 panic、exit=101）。
