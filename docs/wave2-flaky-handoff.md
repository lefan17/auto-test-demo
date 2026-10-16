# 波 2 · P1-1 flaky 治理 — 交接文档

> 基线：`44fc60d`（波 1 收口：口令、契约与实测数字同步到 README 与 CHANGELOG）
> 交付：5 条提交，14 个文件，+1158 / -5（`git diff --numstat 44fc60d..HEAD`；
> 含本文档自身 584 行 —— 不含它则是 13 个文件 +574 / -5）
> 全部实测在 Windows 11 + Python 3.12.10 + pytest 8.3.3 + pytest-rerunfailures 16.7 上完成。

本轮的定位是**让门禁重新变得可信**，不是让红变绿。原始病灶定义（波划分原文）：

> flaky 治理：现状=无 `pytest-rerunfailures`、无隔离区；方向=重跑一次 + 自动打 flaky 标 +
> 周报统计"红但不修"的用例（企业里用例库死亡的头号原因）。

两条不可让渡的约束贯穿全文：

1. **门禁必须保持「真红」可信度** —— 绝不允许全量 blanket rerun。真回归被重跑吞成绿灯，
   门禁就只剩一个装饰性的绿灯（`--reruns` 已被明确排除出 `addopts`）。
2. **不能因为第三方抖动而随机红** —— 33 条打公网 `jsonplaceholder.typicode.com` 的用例，
   CI 实测 4 轮里 3 轮撞上 502/429，`test.yml:198-204` 已如实登记这件事。

---

## 1. 改动清单（改了什么、为什么）

### 新增

| 文件 | 行数 | 作用 |
|---|---|---|
| `testcases/api/flaky_policy.py` | 45 | 公网组的 flaky 策略：`NETWORK_FLAKY` = `pytest.mark.flaky(reruns=1, only_rerun=NETWORK_JITTER_PATTERNS)`，6 条抖动指纹 |
| `scripts/flaky_report.py` | 156 | 读治理账本，产出「重跑才通过 / 重跑仍失败 / 隔离区」三份清单；支持 `--markdown`（贴 step summary）与 `--json`（周报） |
| `testcases/api/test_flaky_governance_meta.py` | 145 | 5 条元测试：开真 pytest 子进程验证机制真的生效 |
| `testcases/api/_probes/probe_flaky_rerun.py` | 28 | 探针：首跑撞 502 文案、次跑通过 |
| `testcases/api/_probes/probe_flaky_real_regression.py` | 20 | 探针：标了 flaky 但失败是**真回归**（字段不匹配），必须当场硬失败 |
| `testcases/api/_probes/probe_quarantine.py` | 18 | 探针：隔离区用例，恒红 |

三个探针**不匹配** `python_files = test_*.py`（前缀是 `probe_` 而不是 `test_`），
所以任何正常的目录递归都收集不到它们——实测 `pytest testcases/api --collect-only` 仍是 33 条。
元测试用显式路径把它们喂给子进程。

### 修改

| 文件 | 行号 | 改动 |
|---|---|---|
| `requirements.txt` | 9-18 | 新增 `pytest-rerunfailures==16.7` + 11 行注释（为什么只 pin 直接依赖 / 为什么**不能**写全局 `--reruns`） |
| `pytest.ini` | 11 | `addopts = -v -ra --strict-markers` → `-v -raR --strict-markers` |
| `pytest.ini` | 23-29 | 新增 `flaky` 与 `quarantine` 两个 marker 的语义说明 |
| `conftest.py` | 89-213 | 新增「重跑留痕 + 隔离区」两段钩子（125 行） |
| `testcases/api/test_post.py` | 6-10 | 挂 `NETWORK_FLAKY` |
| `testcases/api/test_response_time.py` | 6-8 | 同上 |
| `testcases/api/test_user_create.py` | 7-9 | 同上 |
| `testcases/api/test_user_list.py` | 5-7 | 同上 |

#### 1.1 为什么 `addopts` 加了 `R`，却坚决不加 `--reruns`

`-raR` 里那个 `R` 是给 `pytest-rerunfailures` 的：插件只在 `reportchars` 含 `r`/`R` 时才打印
`rerun test summary info` 一节（`pytest_rerunfailures.py` 的 `pytest_terminal_summary`）。
`-ra` 本身不含 `R`，**没有这个字母，重跑在汇总里是隐形的** —— 而「悄悄变绿」正是本轮要治的病。

`--reruns` 反过来是绝对不能写的：它**对每一条用例生效**。真回归（断言写错、接口字段被改坏）
只要第二次侥幸通过就被吞成绿灯。重跑必须由**用例自己声明**，且这条声明能被 grep 到。

实测依据：`get_reruns_count(item)`（`pytest_rerunfailures.py:248-271`）里 marker 优先级高于全局
`--reruns`；`pytest_runtest_protocol`（:1228-1242）拿到 `reruns is None` 时**直接 return**，
插件完全不动手。所以「没标 flaky 的用例一次都不重跑」不是靠约定，是靠插件代码保证的。

#### 1.2 重跑被收窄到「抖动指纹」（`only_rerun`）

`only_rerun` 的匹配对象是 `f"{type(exc).__name__}: {exc}"`（`pytest_rerunfailures.py:651`），
也就是**断言失败信息本身**。`_should_hard_fail_on_error`（:661-686）在失败信息不匹配任何指纹时
直接硬失败，一次都不会重跑。所以 `flaky_policy.py` 里的 6 条正则必须照着
`common/assert_util.py` 实际拼出来的文案写，写错了等于没写：

```python
NETWORK_JITTER_PATTERNS = [
    r"实际状态码: (?:429|5\d\d)",  # assert_status_code 的原文案之一
    r"Max retries exceeded",  # 传输层重试耗尽（apis/base.py 已自带 2 次，这是第二层）
    r"ConnectionError",
    r"Timeout",
    r"响应不是合法 JSON",  # 第三方返回 Cloudflare 错误页/限流页
    r"接口响应过慢",  # test_response_time 的失败文案
]
```

**刻意不含**的指纹（出现即硬失败）：`字段 [...] 不匹配` / `Schema 校验失败` / 类型错误 / 存在重复。
这些是契约断言，红了就是真回归。

#### 1.3 隔离区（`quarantine`）：deselect 但必须打印

`conftest.py` 的 `pytest_collection_modifyitems`：主门禁运行时把带 `quarantine` marker 的项从
`items` 里摘掉，走标准 `config.hook.pytest_deselected`（让 pytest 自己的 "N deselected" 计数
把它们算进去，而不是凭空消失），然后在 `pytest_terminal_summary` 里逐条打印：

```
=========================== quarantine：1 条用例已退出主门禁 ===========================
  DESELECTED testcases/api/_probes/probe_quarantine.py::test_probe_quarantined_known_failure

这些用例既没有被删除，也没有被判为通过——它们的红不再计入本次门禁。
单独运行：pytest -m quarantine
账本（供周报统计）：.../reports/flaky-governance.json
```

两个实现细节都是实测逼出来的，不是设计偏好：

1. **xdist 下 master 侧不跑 `pytest_collection_modifyitems`**（只有 worker 跑），
   所以隔离清单要走 `config.workeroutput` → `pytest_testnodedown` 桥回 master，master 再统一打印。
   worker 自己打印会被丢掉。
2. **判据用词边界正则** `\bquarantine\b` 匹配 `-m` 表达式，而不是子串匹配
   （marker 名字里也可能含这个词）。

#### 1.4 重跑的可见性：为什么自己写账本

要求 4 是「重跑才通过的用例必须能被认出来」。**junit-xml 做不到这件事**：
venv 里 `_pytest/junitxml.py` 的 `pytest_runtest_logreport` 只处理 passed/failed/skipped，
没有 `rerun` 分支；实测重跑的中间报告落成了**重复的 `<testcase>` 节点**，
而最终通过的那个节点**没有任何标记**——只能靠"同名节点出现多次"去猜，且那个副作用会随插件版本变化。

所以 `conftest.py` 的 `pytest_sessionfinish` 自己写一份**治理账本**
`reports/flaky-governance.json`（`reports/` 已被 `.gitignore:10` 忽略，天然不入库；
可用环境变量 `FLAKY_GOVERNANCE_LEDGER` 重定向，元测试就靠它做隔离）：

```json
{
  "schema": 1,
  "generated_at": "2026-10-14T13:19:50+00:00",
  "invocation": "testcases\\api\\_probes\\probe_quarantine.py testcases\\api\\_probes\\probe_flaky_rerun.py -p no:cacheprovider",
  "reruns_total": 1,
  "rerun_passed": [{"nodeid": "testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time", "reruns": 1}],
  "rerun_still_failed": [],
  "quarantined": ["testcases/api/_probes/probe_quarantine.py::test_probe_quarantined_known_failure"],
  "quarantined_count": 1,
  "collected_count": 1
}
```

留痕钩子挂在 master 侧：实测 master 的 `pytest_runtest_logreport` **收得到** worker 的
`outcome == "rerun"` 报告（`report.rerun` 字段也在），所以一个钩子单进程和 xdist 都覆盖。
账本由 master 单写一份——worker 各写各的会互相覆盖，留下哪一份取决于进程退出顺序，
那比没有账本更坏。

#### 1.5 公网组：挂了 flaky 的 33 条

`-m flaky --collect-only` → **33/359**，与 `testcases/api/` 的用例数一致。

---

## 2. 公网组处置：选择与理由

**结论：选 (d) 组合 —— 重跑 + 按抖动指纹收窄 + 保留隔离区出口。**
具体实现是：33 条全部挂 `@pytest.mark.flaky(reruns=1, only_rerun=<6 条抖动指纹>)`，
`quarantine` 机制作为"确实修不动了"的出口存在（当前**没有**任何生产用例真的进隔离区）。

四条路的评估：

**(a) 只加 flaky 重跑（否决）** —— 「重跑」本身不是问题，「重跑的范围」才是。
裸 `@pytest.mark.flaky` 的失败无论什么原因都会重跑一次，于是「接口字段被改坏」这种真回归
只要第二次侥幸通过就会被吞成绿灯。这正是任务书硬约束要防的东西。
所以本轮**没有只做 (a)**，而是做 (a) + `only_rerun` 收窄 —— 重跑只对抖动指纹发生，
契约类断言红了当场硬失败。这两者的差别有元测试守着（见 §5 对照 B）。

**(b) 拆独立 marker 在 CI 单独跑、失败不阻断（否决）** —— 这等于把这 33 条用例的
**门禁权力全部交出去**。它们不只是"打公网的用例"，它们断言的是真实 HTTP 服务上的
分页语义、404 行为、字段类型——这些断言现在再也不会被发现失效了。
用「不红」换来的东西是「不被信任」，而门禁的价值全部建立在被信任上。
附带问题：CI 里新增一个 `continue-on-error: true` 的 step 需要改 `test.yml`（不在本次边界内），
且它一旦长期红着，就变成了需要人肉忽略的噪音。

**(c) 让这批用例可指向本地 mock（否决）** ——
- **真实度损失是根本性的**：这批用例的价值恰恰在于被测对象是**一个不受我们控制的真实 HTTP 服务**。
  换成自家 mock 就是自证自话——mock 的字段是我们自己写的，断言"字段类型对"永远成立。
- **工作量与边界**：`data/config.yaml`（`base.api`）不在本次改动边界内，要做得先改配置加载、
  再造一套 mock 路由（分页、404、字段集），并且 `expected_user_count: 10`、`users.json`
  这些数据契约都要重建。改动面远大于本轮边界。
- 这条**不是永久否决**，而是"本轮不做"。若将来公网服务彻底不可用，这是唯一出路，
  代价是这批用例要重新定义它们到底在测什么。

**(d) 组合（采纳）** —— (a) 的收窄版负责吸收第三方抖动，`quarantine` 负责给
"确实修不动/待 product 确认"的用例一个**可见的**出口（而不是删掉或让它天天红）。
两者都不动 `test.yml` 的门禁语义。

一个附带效果要记账：元测试让 `-m api` 从 116 涨到 121。

---

## 3. 统计产物：`scripts/flaky_report.py`

本地一条命令：

```powershell
& $PY scripts/flaky_report.py              # 人读的文本
& $PY scripts/flaky_report.py --markdown   # 贴 $GITHUB_STEP_SUMMARY
& $PY scripts/flaky_report.py --json       # 给周报脚本
& $PY scripts/flaky_report.py --ledger reports/flaky-governance.json
```

退出码：`0` = 账本读到了；`2` = 账本不存在/不是合法 JSON（报错文案直接给出补救命令）。
**刻意不因为"有重跑"就返回非零** —— 这个步骤的定位是观测，不是门禁；把它弄红会重演
"门禁红了但没人信"的循环。

实测输出（`--markdown`，此前那次调用里隔离区为空）：

```
## flaky 治理

- 重跑总次数：**0**（本次调用：`testcases/api/test_flaky_governance_meta.py -p no:cacheprovider`）
- 重跑才通过：**0** 条
- 重跑后仍失败：**0** 条
- 隔离区：**0** 条

### 隔离区用例（红但不修 = 用例库死亡的头号原因）

（无）

> 单独运行隔离区：`pytest -m quarantine`
```

---

## 4. 元测试：机制真的在生效（不是"装了个插件"）

`testcases/api/test_flaky_governance_meta.py` 里 5 条，全部**开真的 pytest 子进程**
（`cwd` = 仓库根，所以真的 `pytest.ini` + 真的根 `conftest.py` + 真的插件全部生效）：

| 用例 | 断言的东西 |
|---|---|
| `test_flaky_marker_reruns_a_transient_failure` | 首跑撞 502 的探针被重跑一次后通过；`returncode == 0`；输出含 `RERUN` 与 `1 rerun`；账本 `rerun_passed` 里有它 |
| `test_plain_failure_is_not_rerun` | 没标 flaky 的失败用例**一次都不被重跑**（`RERUN` 不在输出里，账本 `reruns_total == 0`）——这是「不许 blanket rerun」的底线 |
| `test_real_regression_is_not_swallowed_by_reruns` | 同样是标了 flaky 的用例，失败文案是契约断言 → 当场硬失败、`RERUN` 不出现、失败原因完整保留 |
| `test_quarantine_case_is_deselected_and_listed_in_main_gate` | 隔离用例红着但门禁绿（`returncode == 0` + `1 deselected`），且 `DESELECTED <nodeid>` **被逐条打印** |
| `test_quarantine_cases_can_be_run_on_demand` | `-m quarantine` 单跑 → `returncode == 1`、`DESELECTED` 不出现、账本 `quarantined == []`（隔离区不是洗白区） |

这些探针的失败文案是**照抄 `common/assert_util.py` 实际拼出来的输出**，不是随手编一句好匹配的话
——探针要打的是生产里那条正则。

### 4.1 反向对照（证明元测试在机制被破坏时真的变红）

每次都是「改坏 → 元测试红 → 还原 → SHA256 回到提交时的值」。

**对照 A：把 `flaky_policy.py` 的 `reruns=1` 改成 `reruns=0`**（重跑消失）

```
$ & $PY -m pytest testcases/api/test_flaky_governance_meta.py -p no:cacheprovider -q --no-header
EXIT=1
E       AssertionError: 重跑没有把这条用例救回来：
E         testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time FAILED [100%]
E         E           AssertionError:
E         testcases\api\_probes\probe_flaky_rerun.py:21: AssertionError
E         ======================== 1 failed, 1 warning in 0.19s =========================
```

还原后 `testcases/api/flaky_policy.py` → `7F1CF1066A61F0CABA13CA2A396B3EDDAA91EF867467EEB7EF98D64E81720398`（一致）。

**对照 B：把 marker 改成 `pytest.mark.flaky(reruns=1)`（拆掉 `only_rerun` = 退化成 blanket rerun）**

```
EXIT=1
_______________ test_real_regression_is_not_swallowed_by_reruns _______________
>       assert "RERUN" not in proc.stdout, f"真回归竟然被重跑了：\n{proc.stdout}"
E       AssertionError: 真回归竟然被重跑了：
```

还原后哈希同上（一致）。

**对照 C：把 `conftest.py` 的 `quarantined = [item for item in items if item.get_closest_marker("quarantine")]` 换成 `quarantined = []`**（摘掉 deselect 钩子）

```
EXIT=1
_________ test_quarantine_case_is_deselected_and_listed_in_main_gate __________
E       AssertionError: 隔离用例没有退出主门禁：
E         testcases/api/_probes/probe_quarantine.py::test_probe_quarantined_known_failure FAILED [ 50%]
E         =============== 1 failed, 1 passed, 1 warning, 1 rerun in 0.22s ===============
E       assert 1 == 0
E         testcases\api\test_flaky_governance_meta.py:120: AssertionError
```

还原后 `conftest.py` → `888ED7C0CFD42BE1567BD633F8709456E8B976C3910D6CF386181F4EE8607EC1`（一致）。

三处还原后重跑元测试 → `5 passed, 1 warning`，`EXIT=0`；`git status --porcelain` 空。

### 4.2 在**真实 33 条用例**上的重跑演示（不是只在探针上）

把 `MAX_RESPONSE_MS` 压到 `1`，让 `test_response_time_should_be_acceptable` 必撞「接口响应过慢」指纹：

```
$ $env:MAX_RESPONSE_MS="1"; & $PY -m pytest testcases/api -q
EXIT=1
=========================== rerun test summary info ===========================
RERUN testcases/api/test_response_time.py::test_response_time_should_be_acceptable
FAILED testcases/api/test_response_time.py::test_response_time_should_be_acceptable
FAILED testcases/api/test_response_time.py::test_response_time_should_be_acceptable
============== 1 failed, 37 passed, 1 warning, 1 rerun in 40.18s ==============
```

账本同时记下 `reruns_total: 1` 与 `rerun_still_failed: [test_response_time.py::...]`。
两个语义都在这里：**抖动指纹命中 → 真的重跑**；**重跑也救不回来 → 照实红**。

---

## 5. 自检命令与原始输出

`$PY = C:\Users\lefan\DSH_project\DH_1\fullstack-test-framework\.venv\Scripts\python.exe`，
`PYTHONPATH=C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs`，workdir = 工作副本根。

### ① `& $PY -m ruff check .` → exit 0

```
All checks passed!
```

### ② `& $PY -m ruff format --check .` → exit 0

```
87 files already formatted
```

（这是本文档**写出来之前**的数字。`ruff format` 也会检查 `.md` 里嵌的代码块，
本文档里那段 `NETWORK_JITTER_PATTERNS` 的对齐就被它改过一次，之后是
`88 files already formatted`。整合者按本文档清单重跑时看到的应是 **88**。）

### ③ `& $PY -m mypy`（**无参数**，走 `pyproject.toml` 的 `files`）→ exit 0

```
Success: no issues found in 71 source files
```

（带 `.` 会误报 `Source file found twice under different module names`，别加。
`scripts/` 已在 `[tool.mypy] files` 里，所以新增的 `scripts/flaky_report.py` 也被查了，
**没有**为它新增任何 `[[tool.mypy.overrides]]`。）

### ④ `& $PY -m pytest -m api -n 4 --dist loadfile` → exit 0

```
====================== 121 passed, 4 warnings in 26.70s =======================
```

（基线 116 passed，+5 = 元测试。本节的每条命令都在**最终提交的工作树**上重跑过
（跑完 `git status --porcelain` 为空），exit code 全部为 0；耗时以首次记录为准。）

### ⑤ `& $PY -m pytest -m mes` → exit 0

```
=============== 54 passed, 305 deselected, 1 warning in 22.91s ================
```

### ⑥ `& $PY -m pytest -m login` → exit 0

```
================ 29 passed, 330 deselected, 1 warning in 2.32s ================
```

### ⑦ `& $PY -m pytest -m contract` → exit 0

```
======== 213 passed, 135 deselected, 11 xfailed, 4 warnings in 23.25s =========
```

### ⑧ CI 等价命令（含覆盖率门禁）→ exit 0

```powershell
$env:MAX_RESPONSE_MS="8000"
& $PY -m pytest -m api -n 4 --dist loadfile --cov=. --cov-report=term --cov-report= --cov-fail-under=40
```

```
TOTAL                                                   4024   1860    54%
Required test coverage of 40% reached. Total coverage: 53.78%
====================== 121 passed, 4 warnings in 35.74s =======================
```

（数字与 `test.yml:255-256` 登记的 `52.23%（3883/1855）` 不同：分母涨到 4024/1860
是因为本次改动新增了 5 条元测试与 3 个探针模块。门禁 40 仍有 13.78pp 余量。）

### ⑨ 元测试单跑 → exit 0

```
======================== 5 passed, 1 warning in 8.67s ========================
```

### ⑩ `& $PY scripts/flaky_report.py --markdown` → exit 0

见 §3。

### ⑪ 机制规模（`--collect-only`）

```
& $PY -m pytest --collect-only -q                          →  359 tests collected
& $PY -m pytest --collect-only -q -m flaky                 →  33/359 tests collected (326 deselected)
& $PY -m pytest --collect-only -q -m quarantine            →  no tests collected (359 deselected)
```

（354 → 359 是 +5 条元测试。`-m quarantine` 目前为空是**预期**的：真实用例库里还没有人进隔离区，
机制的证明在探针上，见 §4。）

### ⑫ xdist 下隔离清单桥接 → exit 0

```powershell
& $PY -m pytest testcases/api/_probes/probe_quarantine.py testcases/api/_probes/probe_flaky_rerun.py -n 2 --dist loadfile
```

```
[gw0] [100%] RERUN testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time
[gw0] [100%] PASSED testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time
  DESELECTED testcases/api/_probes/probe_quarantine.py::test_probe_quarantined_known_failure
=================== 1 passed, 2 warnings, 1 rerun in 2.16s ====================
```

（`-n 2` 下隔离清单照样被打出来，且账本同时收到 rerun 与 quarantine 两件事。）

---

## 6. 给调度者的整合清单

### 6.1 `.github/workflows/test.yml`（**必须加**，否则统计产物等于没接线）

改动只有两处新增 step，**不要动任何既有 step 的门禁语义**。建议插在
「运行登录契约自动化」（`test.yml:330-331`）之后、「生成 Allure 报告」（:343）之前：

```yaml
      # flaky 治理账本 → 流水线摘要 + 周报。
      # 这一步**故意不设 if: failure()**：抖动与隔离区在绿的时候也必须被看见，
      # 只在失败时统计等于"平时不记账、出事才翻账"。
      # 它也不设 continue-on-error —— flaky_report.py 只在账本读不到时返回 2，
      # 而账本由根 conftest.py 的 sessionfinish 钩子无条件写（即使有测试失败）。
      - name: flaky 治理摘要（重跑才通过 / 隔离区）
        if: always()
        run: python scripts/flaky_report.py --markdown >> "$GITHUB_STEP_SUMMARY"
```

**可选**：把账本本身作为 artifact 上传，供周报脚本消费（`--json` 是它的消费格式）：

```yaml
      - name: 上传 flaky 治理账本
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: flaky-governance
          path: reports/flaky-governance.json
          if-no-files-found: warn
          retention-days: 14
```

### 6.2 `requirements.lock`（**必须重建**）

我按约束只改了 `requirements.txt`。重建命令：

```
pip-compile --output-file=requirements.lock --strip-extras requirements.txt
```

实测 `pytest-rerunfailures==16.7` 的元数据只声明两个下界：

```
Requires-Python: >=3.10
Requires-Dist: packaging>=17.1
Requires-Dist: pytest!=8.2.2,>=8.2
```

lock 里已有的 `pytest==8.3.3` 与 `packaging==26.3` 都满足它们，**预期 lock 只新增一行
`pytest-rerunfailures==16.7`，不出现其它版本被顶**。重建后请确认这一点。

> `security` job 扫的是 `requirements.lock`（`test.yml:512-522`）。不重建 lock 的话，
> 新依赖不会被 pip-audit 覆盖到。

### 6.3 `test.yml:198-204` 那段注释（**建议改写**）

原文登记「处置放在波 2 的 flaky 治理（重试策略 / 隔离公网组），本轮不掩盖它」。
本轮已经处置完，建议替换为：

```yaml
      # ✅ 波 2 已处置（docs/wave2-flaky-handoff.md）：这 33 条挂了
      # testcases/api/flaky_policy.py 的 NETWORK_FLAKY =
      #   pytest.mark.flaky(reruns=1, only_rerun=<6 条抖动指纹>)
      # 它们的 502/429/超时会被重跑一次吸收，且**只在**抖动指纹上重跑 ——
      # 契约断言（字段被改坏、schema 漂移）红了仍然当场硬失败，一次都不重跑。
      # 所以这一步的红现在重新可以被当作"代码或契约坏了"。
      # ⚠️ 重跑过的用例不会悄悄变绿：-raR 里的 R 让插件打印 rerun 摘要，
      # 根 conftest.py 另外把账本写进 reports/flaky-governance.json。
```

### 6.4 `README.md`（建议，不在我的边界内）

marker 一节加两行：

```markdown
- `pytest -m flaky` — 允许重跑吸收抖动的用例（当前 = `testcases/api/` 打公网的 33 条）。
  重跑**只对第三方抖动指纹**生效（429/5xx、连接失败、超时、第三方返回非 JSON、响应过慢），
  契约类断言红了会当场硬失败，一次都不重跑。重跑过的用例会在输出里出现 `RERUN`
  与 `rerun test summary info`，并记进 `reports/flaky-governance.json`。
- `pytest -m quarantine` — 隔离区：明知红/不稳定但暂不删除的用例。主门禁运行时被 deselect
  （不是判为通过），但清单会在终端摘要里逐条打印、并写进治理账本。
  `python scripts/flaky_report.py --markdown` 产出的就是「重跑才通过 / 隔离区」两份周报清单。
```

### 6.5 `CHANGELOG.md`（建议原文）

```markdown
### 新增 —— flaky 治理（波 2）
- `requirements.txt` 新增 `pytest-rerunfailures==16.7`。
- `pytest.ini` 新增 `flaky` 与 `quarantine` 两个 marker；`addopts` 由 `-ra` 改为 `-raR`
  （插件的 rerun 摘要只在 reportchars 含 r/R 时打印，否则重跑在汇总里是隐形的）。
  **`addopts` 里不加 `--reruns`**：全局重跑会把真回归一起吞掉。
- `testcases/api/flaky_policy.py`：`NETWORK_FLAKY` 用插件的 `only_rerun` 把重跑收窄到
  6 条抖动指纹，`testcases/api/` 的 33 条公网用例全部挂上。CI 实测 4 轮 3 轮撞上的
  502/429 抖动现在被重跑吸收，而契约断言红了仍当场硬失败。
- `conftest.py`：重跑留痕 + 隔离区两个钩子。重跑才通过 / 重跑仍失败 / 被隔离三类用例
  写进 `reports/flaky-governance.json`（junit-xml 本身不认 rerun，只能自己记）；
  隔离区用例在主门禁里 deselect，但清单逐条打印。
- `scripts/flaky_report.py`：从账本产出「重跑才通过 / 隔离区」清单，支持 `--markdown`
  （贴 step summary）与 `--json`（周报）。本地一条命令即可跑出。
- `testcases/api/test_flaky_governance_meta.py`：5 条元测试，开真 pytest 子进程验证
  「标了 flaky 的用例首跑失败会被重跑」「真回归不被重跑吞掉」「隔离区用例退出主门禁且清单被打印」
  「隔离区能单独跑」。三条机制各配一次反向对照（改坏 → 红 → 还原 → 哈希一致）。
```

### 6.6 不需要改的东西

- **`.gitignore`**：`reports/` 已在 `.gitignore:10`（实测 `git check-ignore -v reports/flaky-governance.json`
  → `.gitignore:10:reports/`），账本天然不入库。
- **`pyproject.toml`**：`[tool.mypy] files` 已含 `testcases` 与 `scripts`，新文件自动被覆盖；
  本轮**没有**新增任何 mypy override，`mypy` 是干净的 71 files。
- **`ruff.toml`**：新文件全部符合既有规则，无需 per-file-ignore。

---

## 7. 未做到 / 不确定的地方

1. **`.github/workflows/test.yml` 未改**（任务书禁改）。整合清单在 §6.1/6.3 ——
   **3 处**：新增 flaky 摘要 step（可选再加账本 artifact）、重建 lock、改写 198-204 的注释。
   摘要 step 不进 CI，`flaky_report.py` 就只是一个本地脚本，周报无从生成。
2. **`requirements.lock` 未重建**（约束是只写 `requirements.txt`）。见 §6.2。
   我按插件元数据推断「只新增一行」，**没有实际跑 pip-compile 验证**。
3. **`README.md` / `CHANGELOG.md` 未改**（禁改）。建议原文在 §6.4 / §6.5。
4. **`-m quarantine` 目前 0 条用例，且空跑会被 pytest 判为「no tests collected」(exit=5)。**
   这是本轮最重要的**未决运维问题**：如果在 CI 里加一个 `pytest -m quarantine` 的 step，
   隔离区为空的那天它会红（GitHub Actions 把 exit=5 当失败）。
   处理选项（**我没有替你们选**，因为涉及"隔离区该不该有自己的 CI step"这个流程决定）：
   - 加 `--co` 只收集不执行，再靠账本统计；
   - 或在 step 里加 `|| [ $? -eq 5 ]`（把「没有隔离用例」当成功）；
   - 或干脆不给隔离区单独的 CI step，只靠 `flaky_report.py` 的周报清单。
   **本轮我只交付了机制**（`-m quarantine` 能单跑、清单会被打印），没给它接 CI。
5. **元测试用的是 `subprocess` + 真 pytest**，比普通单测慢：5 条合计约 9-13 秒，
   占 `-m api`（26.70s）的三分之一到一半。这是有意付的代价 ——
   「装了插件」和「机制真的生效」只有跑真链路才分得清。若嫌慢，可以把它们挪到独立 marker，
   但那样它们就不再是默认门禁的一部分了。
6. **抖动指纹清单是经验性的，不是穷尽的。** 6 条正则照着 `common/assert_util.py` 的实际文案写，
   覆盖 429/5xx、连接失败、超时、非 JSON 响应、响应过慢。**没覆盖到的第三方故障形态
   （例如 TLS 握手失败、DNS 解析失败）会让用例当场硬失败**——这是我刻意的默认方向：
   宁可偶发红一次让人来看，也不要因为漏写一条就把真回归吞掉。真遇到了再加指纹。
7. **6 条指纹里 `Timeout` 与 `ConnectionError` 是较宽的模式**，理论上可能匹配到
   与第三方无关的失败信息。目前 `testcases/api/` 的断言文案里不会出现这两个词，
   但新增用例时值得留意。
8. **没有对 33 条公网用例做真实的长周期抖动采样。** 「4 轮里 3 轮撞上」是波 1 交下来的
   现场记录，本轮没有重新采一遍（成本高且依赖公网当时的状态）。
   本轮在真实 33 条上做的是**定向复现**（§4.2，压 `MAX_RESPONSE_MS` 逼出「响应过慢」指纹），
   证明了重跑链路在真实用例上生效，但没有证明「抖动的实际吸收率」。
9. **隔离区的"周报统计红但不修"只是清单，没有跨周的去重/计数。**
   `flaky_report.py` 每次调用只反映**当次账本**（`invocation` 字段记录了当次命令）。
   要统计"同一条用例连续几周在隔离区里"，需要 CI 侧保留历史账本（例如上传 artifact 后
   由外部脚本聚合）。这超出了本轮边界，也是 §6.1 里建议上传账本 artifact 的原因。
10. **未验证在 Linux/CI 上的行为。** 全部实测在 Windows 11 + Python 3.12.10 上完成。
    已知一个平台敏感点：元测试用 `sys.executable` 起子进程，工作副本里没有 `.venv`
    所以回落到宿主解释器（实测正常）；整合回原仓库后该路径下有 `.venv`，
    行为会切换成那个解释器 —— 在 CI 上请确认 `sys.executable` 与安装 `requirements.txt`
    的环境是同一个（`test.yml` 里是 `python -m pip install` + `python -m pytest`，一致）。
11. **`--cov-fail-under=40` 的分母因为本轮改动变大了**（3883→4024 statements），
    覆盖率从 52.23% 涨到 53.78%。方向是好的，但要知道这个变化来自新增测试文件本身，
    不是被测代码的覆盖变好。

---

## 8. 提交记录（在 `44fc60d` 之上 5 条）

```
fad1920 docs(flaky): 补波 2 flaky 治理交接文档
502c254 docs(deps): 订正 pytest-rerunfailures 的依赖说明
6f745c2 test(flaky): 补元测试与探针，并在机制被破坏时真的变红
efd5885 feat(api): 公网组用例按抖动指纹重跑，真回归仍当场硬失败
101ffa6 feat(flaky): 只对显式标了 flaky 的用例重跑，并按抖动指纹收窄
```

全部通过仓库自带的 `scripts/check_commit_msg.py`（`exit=0`）。
`fad1920` 与 `502c254` 都做过 `--amend`（前者是 `ruff format` 改了本文档里嵌的代码块对齐，
后者改了顶部统计行），amend 后 `git status --porcelain` 为空。

`502c254` 是自我订正：`101ffa6` 的注释里写的「插件自己不声明额外的运行期依赖」是错的
（实测 `Requires-Dist: packaging>=17.1` 与 `pytest!=8.2.2,>=8.2`），已改成经得起核对的说法。
