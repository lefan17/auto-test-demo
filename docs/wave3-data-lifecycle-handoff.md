# 波 3 · P2-5 数据生命周期 —— 交付说明

问题原文：**「`data_factory.py` + `data_isolation.py`（8 张表快照/行数校验，做得不错）已经到位，
缺的是失败时的清理兜底与 TTL。」**

基线 `0e61618`，工作副本 `work/w3-data`。本文件记录：现状 → 做了什么（文件:行号）→
为什么这么选 → 整合清单 → 未验证项。

---

## 一、现状（改动前的真实缺口）

### 缺口 1：回收被断言挤掉（这是"失败时没有兜底"的活证据）

`testcases/mes/conftest.py` 的 `factory` fixture 原本是**直线代码**，顺序为
`先验流水链 → 断言 → cleanup()`。基线原文（`0e61618:testcases/mes/conftest.py:81-88`）：

```python
chain_errors = ledger_balance_chain_errors(mes_server.db_path)
assert not chain_errors, ...  # ← 这里一红
actions = f.cleanup()  # ← 这行就永远走不到
```

流水链一红，`cleanup()` 不执行 → 用例造的数据全留在库里 → 会话收尾的
`data_isolation_check` 再红一次。**两个红，真因被第二层噪声盖住**，排查方向被指到别处。
fixture 自己的 docstring 写着「即使断言失败也能收尾」，代码做不到。

### 缺口 2：`cleanup()` 不复核

`testcases/mes/data_factory.py:327-398`（改前 :326-399）执行完 DELETE 直接 `return actions`，
从不检查 `rowcount`、从不回查残留。一次没生效的删除表现为「用例全绿、下一轮基线漂移」——
报错信息指向下一轮，跟真正原因隔了一层。

### 缺口 3：没有任何按时间清理的入口

跨会话残留（Ctrl-C / 崩溃 / CI 超时导致 teardown 没跑）没有任何东西负责。
`stock_ledger` 无删除接口，`stock` 表**没有 `created_at`** 列，都是判据上的坑。

---

## 二、做了什么

### 2.1 失败兜底（交付项 1）

**`testcases/mes/data_factory.py:400-435`** 新增 `verify_clean()`：清理之后再回查一遍库，
按**登记过的**单号/物料编码逐条 `SELECT COUNT(*)`，返回没被删掉的残留清单。

**`testcases/mes/conftest.py:90-119`** teardown 重写为「两件事互不阻断」：

```python
chain_errors = ledger_balance_chain_errors(mes_server.db_path)  # :90  先留证据
cleanup_error: BaseException | None = None
try:
    actions = f.cleanup()  # :93  一定执行
except BaseException as exc:  # :94  覆盖一切中断（含 KeyboardInterrupt）
    actions = []
    cleanup_error = exc
try:
    residues = f.verify_clean()  # :98
except BaseException as exc:
    residues = []
    if cleanup_error is None:
        cleanup_error = exc
if actions:
    print("\n[数据回收] " + "；".join(actions))  # :105
if cleanup_error is not None:
    print(f"\n[数据回收失败] {type(cleanup_error).__name__}: {cleanup_error}")  # :107
    raise cleanup_error  # :108  先打印再原样抛回，不吞
assert not residues, ...  # :109  残留显式报出
assert not chain_errors, ...  # :114  流水链原因的报在最后
```

**为什么捕获 `BaseException` 而不是 `Exception`**：兜底要覆盖 `KeyboardInterrupt`——
「Ctrl-C 打断」正是任务书点名要处理的场景，`Exception` 捕不到它。

**为什么先打印再 `raise`**：静默吞掉就等于让"数据没删干净"永远不可见。
打印保证退出码/stdout 里留下证据，原样抛回保证 pytest 仍然把它记成 error，
不会伪装成绿。

**为什么先报残留、后报流水链**：残留是"回收没生效"，流水链是业务错账。
流水链报在后，是为了让**真因（账实不符）出现在输出的最后一行**，
读日志的人第一眼看到的就是它。

### 2.2 TTL / 过期数据（交付项 2）

新增 **`testcases/mes/data_ttl.py`**（497 行）+ **`scripts/mes_data_ttl.py`**（CLI）。

**判据（测试数据 vs 基线数据）——两级，白名单是硬否决：**

| 级别 | 规则 | 位置 |
|---|---|---|
| ① 基线白名单 | `SEED_USERS` 账号名、`SEED_MATERIALS` 物料编码、`SEED_STOCK` 的 `(物料, 仓库)` **键**。命中即**永不删除，哪怕已过期** | `data_ttl.py:52-54` |
| ② 过期判定 | `created_at` 早于 `now - N 小时`（N 由 `--ttl-hours` 给，默认 24） | `data_ttl.py:83-86`、`data_ttl.py:405` |
| ③ 可识别性 | 工厂标记正则 **或** 显式登记的手写字面量 | `data_ttl.py:45`、`48`、`49` |

只有「过期 **且** 不在白名单 **且** 认得出是测试数据」才进删除集合。

```python
# data_ttl.py:45  工厂生成形状 `{前缀}-{6位随机}-{自增序号}`（见 DataFactory.next/tag）
FACTORY_TAG_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[A-Z0-9]{6}-\d+$")
# data_ttl.py:48-49  用例手写的测试编码：不含随机标记，正则认不出，只能显式登记
KNOWN_TEST_CODES = frozenset({"FG-TEST-01", "FG-X", "FG-NOREPLAY", "MAT-ACL"})
KNOWN_TEST_PREFIXES = ("WO-IDEM-", "WO-CONFLICT-", "WO-DUP-", "WO-FIXED-", "WO-LONGKEY-")
```

**为什么必须有白名单（这是本项最关键的一步）**：种子行的 `created_at` 就是**建库那一刻**，
本来就老于任何 TTL。**只看时间判定，第一次跑清理就会把 5 个种子物料、5 行种子库存全删掉。**
白名单不是"额外保险"，是让"按时间清理"这件事成立的**前提**。

**为什么还要第 ③ 级（不能只靠白名单）**：仓库里有一批用例**手写**测试编码，不含工厂随机标记
（`FG-TEST-01`/`FG-X`/`FG-NOREPLAY`/`MAT-ACL`/`WO-IDEM-*`/`WO-CONFLICT-*`/`WO-DUP-*`/
`WO-FIXED-*`/`WO-LONGKEY-*`）。纯白名单会漏掉它们；纯时间会误删基线。两级缺一不可。
**认不出来的过期行不删、只报告**（`report.unidentified`），交人工确认——
宁可留着被人看到，也不猜着删。

**级联语义**：工单过期时其 `production_batches`/`inspections`/`stock_ledger` 按**父行**判定
一起删；物料过期时其 `stock` 行按物料编码一起删。子行**不**按自身时间判定——
父子时间不一致时会撞外键或留孤儿（`data_ttl.py:22-23` 注释说明）。

**时间戳容错**（`data_ttl.py:64-80`）：库里真实存在**两种**形状——服务端 `now_iso()` 的
`2026-02-14T10:23:45+08:00`（带偏移）与 `force_status()` 里 SQL `datetime('now')` 的
`2026-02-14T02:23:45`（UTC、无偏移、空格分隔）。无偏移的一律按 **UTC** 解释：
当成东八区会让判断整整偏 8 小时。解析不出来的值**按未过期处理**。

**`stock` 表无 `created_at`**（`mock_server/db.py:117-128` 只有 `updated_at`），
所以 stock 行的过期完全由「所属物料/工单是否过期」决定，不看自己的时间。

**`users` 表只报告不自动删**：全库搜 `/users` 零命中，没有用例建账号，
多出来的账号一定出自人工操作（`data_ttl.py:25-26`）。

**安全默认**：不带 `--apply` 就是 **dry-run**，一行都不动。另有 `--json`、
`--list-lingering`、`--fail-on-lingering`（有残留则退出码 3，供 CI 用）。

### 2.3 幂等（交付项 3）

清理只做「删除**已存在且已过期**的行」。第二次跑时那些行已经不在了 →
`items` 为空、行数不变、不报错、不动基线。另有 `baseline_lost` 自报机制：
清理前后比较白名单行数，一旦减少就在报告里打 `!! 基线行数减少了（误删）`
（`data_ttl.py:424-428`、`458-460`）——**这个字段非空就是 bug，报告不许替它遮掩**。

### 2.4 与现有会话级基线断言衔接（交付项 4）

**没有改**会话级断言的语义，一行都没放宽。清理器与会话断言看的是**同一批事实**：
`lingering_rows()`（`data_ttl.py:357-384`）列的正是"非基线行"，
与会话级 `data_isolation_check` 的 `assert before == after` 同源。所以它非空时那条断言必然也红。

**会话级基线断言的准确行号（本副本）**：
- 改动前：`testcases/mes/conftest.py:129`（`assert before == after`）
- 改动后：**`testcases/mes/conftest.py:158`** —— 因为 `factory` teardown 加长了 29 行，断言整体后移。
  任务书/README 提到的 `:129` 与本副本改动前的行号一致，改动后应引用 `:158`。
- fixture 定义：`testcases/mes/conftest.py:140`（`def data_isolation_check`）；
  `:155` `before = snapshot(...)`。

---

## 三、整合清单（我要据此合进原仓库）

| 文件 | 状态 | 改了什么 |
|---|---|---|
| `testcases/mes/conftest.py` | 改 | `factory` teardown 重写（改动后的 `:90-119`）：try/except BaseException 保证 `cleanup()` 必跑；`verify_clean()` 残留断言；失败先打印再抛回。docstring 补「为什么长成这样」（`:79-85`） |
| `testcases/mes/data_factory.py` | 改 | 新增 `verify_clean()`（`:400-435`）；`import sqlite3` 提到模块级（`:32`，原在 `cleanup()` 函数体内） |
| `testcases/mes/data_ttl.py` | **新增** | 497 行。`FACTORY_TAG_RE`(:45) / `is_test_code`(:57) / `parse_ts`(:64) / `PurgeReport`(:121) / `baseline_row_counts`(:255) / `lingering_rows`(:357) / `purge`(:387) / `format_report`(:446) |
| `scripts/mes_data_ttl.py` | **新增** | 102 行 CLI。`--db` / `-n --ttl-hours` / `--apply` / `--json` / `--list-lingering` / `--fail-on-lingering` |
| `testcases/mes/test_data_lifecycle.py` | **新增** | 322 行，9 条用例（子进程故障注入 ×2、TTL 清理、白名单、幂等、模糊项、命名判据、时间解析、`verify_clean`） |
| `testcases/mes/_fault_probe.py` | **新增** | 故障注入探针（**故意红**）：改错流水余额 → 命中真实缺陷 |
| `testcases/mes/_leak_probe.py` | **新增** | 漏登记探针（**故意红**）：绕开工厂造数据 → 被会话级断言抓到 |
| `README.md` | 改 | 「数据隔离」节补 TTL 入口说明；用例数/耗时/覆盖率/mypy 数字同步为实测值 |
| `CHANGELOG.md` | 改 | `[未发布]` 下追加波 3 小节（含探针第一版空转的教训） |
| `docs/测试有效性说明.md` | 改 | 新增第 6 节「故障注入探针第一版是空转」+ 站得住表加一行；第 3 节的 `conftest.py:81-87` 行号加注改后位置；末尾加「计数不回溯」说明 |

**两个探针为什么放在非 `test_*.py` 文件里**：它们**故意是红的**，放进正常收集会破坏
验收标准 1（`pytest -m mes` 全绿）。`pytest.ini` 的 `python_files = test_*.py` 让目录扫描
跳过 `_fault_probe.py`/`_leak_probe.py`；元测试用**显式路径**喂给 pytest，
实测显式路径会绕过该过滤并正常收集（与 `testcases/api/_probes/` 同款范式）。

### 提交

```
7f74b6f  fix(mes): 用例失败时不再让断言挤掉数据回收
739bf59  feat(mes): 新增按 TTL 清理跨会话残留测试数据的入口
9024f82  test(mes): 用真实子进程给数据生命周期五项验收钉住证据
acd7619  docs(mes): 波 3 数据生命周期的交付说明与文档同步
e6668ce  docs(mes): 校正 handoff 里的行号与文件规模
11da8da  docs(mes): 验收 3 的 TTL 证据补上清理前后逐表对照
```

列表截止到 `11da8da`；把本行所在提交自己的 SHA 写进来会因下一次 amend 立刻过期，
所以要一份最新的请直接跑 `git log --oneline 0e61618..HEAD`。

---

## 四、验收证据（原始命令 + 输出关键行）

### 验收 1：正常 `pytest -m mes` 全绿

```
python -m pytest -m mes -q
=============== 64 passed, 314 deselected, 1 warning in 36.04s ================

python -m pytest testcases/mes/test_data_lifecycle.py -q
======================== 9 passed, 1 warning in 11.83s ========================
```

改动前 55 条，新增 9 条（`test_data_lifecycle.py`）。数据回到基线（会话级断言未红）。

### 验收 2：故障注入 —— 用例失败后数据仍被回收

```
python -m pytest testcases/mes/_fault_probe.py -q
E       AssertionError: 流水账面余额与实际库存对不上（账实不符）—— 明细在下面：
E       assert not ['  流水 #1 MAT-CU-001/WH-01 ISSUE 3.000：记录余额 1116.000，按链重放应为 117.000（差 +999.000）', ...]
testcases\mes\conftest.py:114: AssertionError
[数据回收] work_order WO-T-7PR9AI-1 及其批次/质检/流水
==================== 1 passed, 1 warning, 1 error in 2.25s ====================
```

`[数据回收]` 出现（回收真的跑了）、**没有**"会话结束时数据没有回到基线"（没连带污染）、
只有流水链这一条红 —— 真因没被盖住。

（单号里的 `7PR9AI` 是 `factory.tag()` 每轮随机生成的 6 位后缀，跑两次不会一样；
判定要看的是"有没有 `[数据回收]` 这一行"，不是那个 tag。）

**承重性验证（关键）**：把 `conftest.py` 换回基线版本再跑同一条探针：

```
___ ERROR at teardown of test_ledger_chain_failure_does_not_starve_cleanup ____
  | AssertionError: 会话结束时数据没有回到基线 —— 有用例造了数据没清理，或改了库存没退回去。
  |     表 work_orders: 基线 0 行 -> 现在 1 行（差 +1）
  |     表 stock_ledger: 基线 0 行 -> 现在 2 行（差 +2）
  |     流水数量合计: 基线 0.000 -> 现在 6.000（差 +6.000）
  | AssertionError: 流水账面余额与实际库存对不上（账实不符）
==================== 1 passed, 1 warning, 1 error in 2.67s ====================
```

修复前**没有** `[数据回收]`，且出现基线漂移（`work_orders +1`、`stock_ledger +2`）——
证明这条探针确实钉住了修复的那个缺陷，不是一条"怎样都绿"的摆设。

> ⚠️ **踩过的坑（值得记一笔）**：探针**第一版**用的是"用例正文随便抛个异常"。
> 它对着**修复前**的代码也是绿的 —— 因为正文抛异常根本不阻止 teardown 继续往下跑，
> `cleanup()` 照样执行。**等于什么都没证明。** 真正会挡住 `cleanup()` 的只有
> "先断言、后回收"那个顺序，所以探针必须精确命中流水链断言。
> 如果只看"我写了故障注入用例且它红了"就收工，这条交付会是空的。

### 验收 3：TTL 清理识别超期测试数据、不动基线

临时库的造法：`mock_server.db.configure(tmp)` 后 `init_db(reset=True)` 写入**真种子**，
再用一条直连 sqlite3 的脚本注入 1 条过期（`created_at` 回拨 48 小时）工单
+ 其过期批次 + 1 条过期物料及其库存行（`WO-T-OLD001-1` / `MAT-T-OLD002-2`，
形状按工厂规则，确保会被判据认出来）。脚本本身是临时取证用，**没有进仓库**。

第一次实测 **BEFORE**（8 表行数）：

```
BEFORE {'users': 5, 'materials': 6, 'work_orders': 1, 'production_batches': 1,
        'inspections': 0, 'stock': 6, 'stock_ledger': 0, 'idempotency_keys': 0}
```

```
python scripts/mes_data_ttl.py --db <tmp> -n 24            # dry-run，exit=0
  口径：早于 2026-10-14T12:01:28+08:00（TTL 24 小时）
  模式：试运行（dry-run，未改动数据）；命中的过期测试数据 4 项 / 4 行
    - work_orders: WO-T-OLD001-1 —— 1 行（工单已过期）
    - production_batches: 1 行 —— 1 行（所属工单已过期）
    - stock: 1 行 —— 1 行（所属物料/工单已过期；种子库存按 (物料, 仓库) 键受保护）
    - materials: MAT-T-OLD002-2 —— 1 行（物料已过期且不在种子名单）
  受白名单保护的行数：{'users': 5, 'materials': 5, 'stock': 5}
  非基线残留 4 处（试运行未删（含上面已命中的过期项））：
  ...

python scripts/mes_data_ttl.py --db <tmp> -n 24 --apply   # 第一次，exit=0
  模式：已执行删除；命中的过期测试数据 4 项 / 4 行
  受白名单保护的行数：{'users': 5, 'materials': 5, 'stock': 5}
    materials: 6 -> 5
    work_orders: 1 -> 0
    production_batches: 1 -> 0
    stock: 6 -> 5
  非基线残留：无（库已回到基线口径）
```

**基线对照数字（清理前后）**：

| 表 | BEFORE | AFTER | 判定 |
|---|---|---|---|
| `users` | 5 | 5 | 种子白名单，**未动**（`受白名单保护` 里报 5） |
| `materials` | 6 | 5 | 只删了 1 条过期测试物料，5 条种子**未动** |
| `stock` | 6 | 5 | 只删了测试物料的库存行，5 行种子库存按 `(物料, 仓库)` 键**未动** |
| `work_orders` | 1 | 0 | 过期测试工单被删 |
| `production_batches` | 1 | 0 | 随父工单级联删 |
| `inspections` / `stock_ledger` / `idempotency_keys` | 0 | 0 | 本来就没有 |

dry-run 里那句"非基线残留 4 处"是**如实的**：试运行没删，上面命中的 4 项当然还在库里。
措辞在 `data_ttl.py:471-473` 特意区分了"试运行未删"与"未过期/未确认不删"——
否则那 4 行会被读成"这些永远不会删"。

### 验收 4：幂等（连跑两次）

```
python scripts/mes_data_ttl.py --db <tmp> -n 24 --apply   # 第二次
  模式：已执行删除；命中的过期测试数据 0 项 / 0 行
    （没有命中的过期测试数据）
  受白名单保护的行数：{'users': 5, 'materials': 5, 'stock': 5}
    行数未变化（清理幂等：没有可删的过期数据）
  非基线残留：无（库已回到基线口径）
exit=0
```

第二次：退出码 0、无报错、0 项命中、行数不变、基线未动。

### 验收 5：清理不彻底 → 会话级基线断言会红

```
python -m pytest testcases/mes/_leak_probe.py -q
_ ERROR at teardown of test_unregistered_data_is_caught_by_session_isolation_check _
>       assert before == after, (
E       AssertionError: 会话结束时数据没有回到基线 —— 有用例造了数据没清理，或改了库存没退回去。
E           表 work_orders: 基线 0 行 -> 现在 1 行（差 +1）
testcases\mes\conftest.py:158: AssertionError
==================== 1 passed, 1 warning, 1 error in 2.43s ====================
```

断言精确报出「表 work_orders: 基线 0 行 -> 现在 1 行（差 +1）」。

### 门禁

```
python -m ruff check .          → All checks passed!
python -m ruff format --check . → 99 files already formatted     （改动前 93）
python -m mypy                  → Success: no issues found in 79 source files   （改动前 74）
```

---

## 五、未验证项 / 边界（必须知道的）

1. **TTL CLI 没在"真实手工库"上做过 `--apply`**。所有验收都在 `tmp_path` 造的临时库上。
   `mock_server/dsh_mes.db` 只跑过 `--list-lingering`（只读）。**首次对真实库动手前先跑 dry-run**。
2. **`users` 表永不自动删除**，只在报告里列出多出来的账号。这是刻意的（风险大于收益），
   但它意味着"多出账号"这类污染**不会被清理器自动修好**。
3. **领料/退料对基线物料只改数值、不改行数**（`stock_available_milli`）。
   TTL 清理**不负责**恢复数值，只清残留行。数值漂移仍然只由会话级断言的
   "关键数值列合计"指标抓（`test_mes_business.py:26-67` 的 `take_material` teardown）。
   → **若有人绕过该 fixture 领料且不退料，TTL 清理不覆盖这种污染。**
4. **`idempotency_keys` 的清理依赖时间**：该表基线 0 行，但行只在过期后才会被删。
   未过期但已"用完"的幂等键会留在库里（会话级断言在会话内是绿的，因为用例自己登记回收了）。
5. **跨会话残留在本仓库 fixture 下不会发生**（session fixture 每次 `mktemp` + `--reset` 新库）。
   TTL 入口的真实价值场景是：开发者手工跑服务的那份 `mock_server/dsh_mes.db`，以及 CI 复用库。
   → **它不是一个"会被自动调用"的东西**，需要显式跑 CLI（或用 `--fail-on-lingering` 接进 CI）。
6. **`KNOWN_TEST_PREFIXES` / `KNOWN_TEST_CODES` 是手工维护的清单**。用例里新增手写测试编码时
   必须同步登记，否则那些行会落进 `unidentified`（只报告不删）。这是**刻意的保守**，
   但也是一个维护负担 —— 已在 `data_ttl.py:47-49` 注释说明。
7. **`docs/测试有效性说明.md` 已补第 6 节**（探针空转的教训 + 站得住表新增一行），
   但**没有重跑前 5 节的变异实验**——那几节的 `passed` 计数是写作当时的实测值，
   末尾已加「计数不回溯」的说明。**本波没有验证前 5 节结论今天仍然成立**。
8. **并行模式已复测但只跑过一次**：`pytest -m mes -n 4 --dist loadfile` → `64 passed in 18.98s`。
   TTL 用例有自己的 `tmp_path` 库，与并行无关；但**没有在多轮重复下压测过**。
9. **README 里的用例数已同步为实测值**（`--collect-only` → 378 条、`-m mes` → 64 条、
   覆盖率 `-m "not ui"` → 90%（89.63%）、`-m api` 门禁 → 75.69%）。
   其中 `docs/wave2-coverage-handoff.md` 里仍有波 2 留下的旧数字（55 条 / 300 条），
   **本波未回改历史 handoff** —— 历史文档按当时的实测写，这是刻意的。
10. **`scripts/mes_data_ttl.py` 没有被任何自动化调用**。仓库里没有 CI 定时任务、
   也没有 pytest 钩子在会话收尾时调它。它是一条**手工命令**（或由使用者在 CI 里自行接线）。
    → 这是刻意的：任务书要求「清理不彻底必须能被现有断言抓到」，
    **在会话收尾自动清扫等于把那个断言洗绿**，属于放宽断言。
11. **`ruff format` 会改 Markdown 代码块**。本波首次在 `docs/` 里写了带注释的 Python 代码块，
    `ruff format --check .` 直接把它们判为未格式化（`ruff 0.16.10`）。
    → 新增含代码块的文档后要跟着跑一次 `ruff format`，否则门禁红。
    **此前波 1/波 2 的 handoff 没有被这样处理过**（它们的代码块恰好满足 format 的形态）。
