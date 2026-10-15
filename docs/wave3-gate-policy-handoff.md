# 波 3 · P2-4 分层门禁策略显式化 —— 交接说明

**一句话**：把「哪个测试层阻塞 PR、哪一步放行」从 `test.yml` 的 `continue-on-error` 行里搬出来写成
`docs/质量门禁策略.md`，并新增 `scripts/check_gate_policy.py` 让文档与 workflow / `pytest.ini`
**可机器校验地对齐**——两边一分叉，CI 立刻红。

范围：6 个中文 conventional commit（`7b6a044` → `d024a42`），7 个文件 +707/-4，
**没有任何现有 job 从阻塞被改成非阻塞**。

---

## 一、现状（改动前）

| 事实 | 位置（改动前） |
|---|---|
| `pytest.ini` 共 33 行，9 个 marker（smoke/regression/api/ui/mes/login/contract/flaky/quarantine） | `pytest.ini:15-29` |
| `.github/workflows/test.yml` 653 行，6 个 job | `lint`(35) `typecheck`(76) `test`(107) `security`(519) `pre-commit`(574) `commit-convention`(618) |
| 全 workflow 只有 2 处 `continue-on-error: true` | `test.yml:299`（UI step）、`test.yml:546`（pip-audit step） |
| 「哪些阻塞」只散落在 README 的表格与 workflow 的注释里 | `README.md:497-528`、`CONTRIBUTING.md:128-129` |
| `CONTRIBUTING.md` 那句把 job 名与 marker 名混用且漏项 | 原文：「`lint` / `typecheck` / `mes` / `login` 是阻塞的；`-m ui` 与 `security`（pip-audit）非阻塞」——`mes`/`login` 是 marker 不是 job，`pre-commit`/`commit-convention` 漏了 |
| UI 不阻塞的理由只有一句「打的是公共 demo 站，外部因素多」 | `test.yml:292-296`、`README.md:515-517` |
| 隔离区（quarantine）机制已实现，但只是 workflow 里注释交代 | `test.yml:356-359`、根 `conftest.py:126-213` |

规模（实测，`pytest --collect-only -q -m <marker> -o addopts=`，全量收集 369）：
`api` 130 / `contract` 224 / `mes` 55 / `login` 29 / `ui` 14 / `smoke` 36 / `regression` 64 / `flaky` 33 / `quarantine` 0。

---

## 二、做了什么（文件:行号）

### 1. `docs/质量门禁策略.md`（新增，267 行）

| 节 | 行号 | 内容 |
|---|---|---|
| 一、触发时机 | 12-29 | 4 个触发源：`push main` / `pull_request` / `schedule "0 18 * * 1"`（UTC 周一 18:00 = 北京周二 02:00）/ `workflow_dispatch`。**6 个 job 在这 4 种触发下全部执行**（实测所有 job 都没有事件级 `if:`），夜间回归的价值是「同一套用例在无人改代码时再跑一遍」 |
| 二、job 表 | 31-55 | 机器可读段 `<!-- gate-policy:jobs -->`（33/42 行）。6 行 ×（job / 阻塞性 / 预算分钟 / 触发时机 / 失败找谁 / 判定步骤） |
| 三、marker 表 | 57-93 | 机器可读段 `<!-- gate-policy:markers -->`（64/76 行）。9 行，并写明 **marker 分三类**：`api`/`ui`/`mes`/`login`/`contract` 是宿主层，`smoke`/`regression` 是横切标签，`flaky`/`quarantine` 是质量治理标签 |
| 四、放行清单 | 95-114 | 机器可读段 `<!-- gate-policy:continue-on-error -->`（100/105 行）。**全 workflow 只有 2 处豁免，逐条登记**；并写明「加严是单向的」 |
| 五、flaky / quarantine 承接 | 116-174 | 见本 handoff 第五节 |
| 六、UI 不阻塞的理由/风险/退出条件 | 176-228 | 见本 handoff 第七节 |
| 七、预算时长 | 230-246 | 6 个 job 的 `timeout-minutes` 与实测参照 |
| 八、怎么用、怎么改 | 248-267 | 脚本用法与「改 workflow 必须同一 PR 改文档」的流程 |

三张表全部夹在 HTML 注释锚点里（doc 41-45 行的设计说明）：
锚点用注释而不是标题文字，**因为标题会被人顺手改掉，改完之后脚本静默找不到段落就退化成「什么都没检查」**。

### 2. `scripts/check_gate_policy.py`（新增，376 行）

零依赖（只 import `argparse/re/dataclasses/pathlib/sys`），四项**双向**校验：

| 函数 | 行号 | 判定规则 |
|---|---|---|
| `parse_pytest_markers` | 102-125 | 取 `markers =` 块里缩进恰好 4 空格、形如 `名字:` 的行 → `{marker: 行号}`；块在第一个不缩进行结束；**解析不到任何 marker 直接 `SystemExit`**（不静默通过） |
| `parse_workflow` | 128-171 | 纯缩进扫描：顶格 `jobs:` 之后缩进 2 且以 `:` 结尾 = job（`doc 136` 行）；缩进 6 的 `- name:` = step（`doc 156-158`）；缩进 4 的 `timeout-minutes:` = 预算（`doc 163-166`）；缩进 8 的 `continue-on-error:` = 豁免点（`doc 167-169`） |
| `section_rows` | 174-200 | 取 `<!-- section -->` … `<!-- /section -->` 之间的表格数据行；**找不到锚点抛 `SystemExit`**（`doc 186-187`） |
| `check_job_sets` | 256-270 | ① job 集合双向对称：workflow 有而文档没有 → `FAIL … 新增 job 必须同时登记策略`；文档有而 workflow 没有 → `FAIL` |
| `check_blocking` | 273-297 | ② 阻塞性：`expected = RELAXED_VALUES if job.relaxed_steps else (BLOCKING,)`（`doc 278`）——**「阻塞」的判据是客观的：该 job 内没有任何 `continue-on-error: true` 的步骤**；同时比对 `timeout-minutes` |
| `check_relaxed_steps` | 300-320 | ③ 放行步骤清单双向对称：workflow 新出现一处豁免而文档没登记 → `FAIL`；文档登记了而 workflow 里已不再豁免 → `FAIL` |
| `check_markers` | 323-339 | ④ marker 归属双向对称：`pytest.ini` 里每个 marker 都要在文档 marker 表里有归属，反之亦然 |
| `main` | 342-372 | 有失败条目则逐条打印并 `return 1`；否则 `return 0` |

**为什么不用 PyYAML**（`doc 131-132` 的注释是同一件事）：这份 workflow 只有 job/step 两层结构，
而 YAML 1.1 会把顶格的 `on:` 解析成布尔 `True`（实测 `data["on"]` → `KeyError: 'on'`，
`data[True]` 才是触发块），还得为它写补偿代码；缩进扫描更直白，也让脚本零依赖。
**实测两种解析方式对 6 个 job 的 `timeout-minutes` 与豁免点判定完全一致**（探针 `MATCH = True`）。

### 3. `.github/workflows/test.yml`（+8 行，只加严）

在 `pre-commit` job 的 `pre-commit run --all-files` **之后**新增一步（`test.yml:610-616`）：

```yaml
      - name: 校验门禁策略与 workflow 一致
        run: python scripts/check_gate_policy.py
```

没有动任何已有 job 的阻塞性，没有动任何 step 的 `continue-on-error`。
**为什么挂在 `pre-commit` job 而不是新开 job**：它是纯文本比对、秒级返回，新开 job 只会多付一次
runner 启动与 Python 安装的钱；挂在已有的阻塞 job 里，它天然继承阻塞语义，且**不可能**被顺手设成非阻塞。

### 4. `pytest.ini`（+5 行，仅注释）

在 `markers =` 上方（`pytest.ini:15-19`）加 5 行注释，指明 marker 的归属文档与「新增/改名 marker
必须同时改文档，否则 CI 红」，并点明 `smoke` / `regression` / `flaky` 是横切标签、CI 里没有各自的 `-m` step。
**marker 名单（9 个）与 `addopts = -v -raR --strict-markers` 逐字节未变**（`git diff` 只有新增行）。

### 5. `README.md`（+14/-2）、`CONTRIBUTING.md`（+5/-2）、`CHANGELOG.md`（+32）

- `README.md:501-504`：新增一段指向策略文档，声明「那份文档不是说明文，是门禁的一部分」。
- `README.md:512`：`pre-commit` 行补上「再跑 `scripts/check_gate_policy.py`」。
- `README.md:526-529`：UI 那段从一句「外部因素多」改为列出三类风险与三条退出条件并指向第六节。
- `README.md:136-139`：flaky/quarantine 段补指向第五、六节。
- `CONTRIBUTING.md:128-132`：把混用 job 名/marker 名且漏项的原文改为「以策略文档为准」+ 准确的 6 job 速览。
- `CHANGELOG.md:15-45`：「未发布」段新增 `### 新增 —— 分层门禁策略显式化（波 3）`，含改动、反向验证的红证与实测数字。

---

## 三、check_gate_policy.py 的校验规则与被破坏时会怎样

| 破坏动作 | 脚本反应（实测原文） | 退出码 |
|---|---|---|
| **A. 把 job 名 `security` 改成 `security-scan`，不动文档** | 4 项：`FAIL job security \| 策略文档第 39 行声明了它，但 workflow 里已经没有这个 job`；`FAIL job security-scan \| workflow 第 519 行有它，策略文档的 job 表里没有——新增 job 必须同时登记策略`；`FAIL security / pip-audit（扫描锁定的依赖树） \| 策略文档第 104 行登记了它，但 workflow 里没有这一步（或它已不再放行）`；`FAIL security-scan / pip-audit（扫描锁定的依赖树） \| workflow 第 546 行是 continue-on-error: true，策略文档的放行清单里没有登记` | 1 |
| **B. 只删掉 UI 那步的 `continue-on-error: true`，不动文档** | 2 项：`FAIL test 阻塞性 \| 文档第 38 行写「部分放行」，但 workflow 里没有 continue-on-error（无）→ 应写「阻塞」`；`FAIL test / 运行 UI 自动化（失败不阻断流水线） \| 策略文档第 103 行登记了它，但 workflow 里没有这一步（或它已不再放行）` | 1 |
| **C. 把某个阻塞 job 改成非阻塞**（实验：给 `lint` 的 `ruff check` 那步加 `continue-on-error: true`，不动文档） | 2 项：`FAIL lint 阻塞性 \| 文档第 36 行写「阻塞」，但 workflow 里有 continue-on-error（第 65 行 'ruff check（lint 规则）'）→ 应写「部分放行 或 不阻塞」`；`FAIL lint / ruff check（lint 规则） \| workflow 第 65 行是 continue-on-error: true，策略文档的放行清单里没有登记` | 1 |
| **D. 把 `pytest.ini` 里的 marker `quarantine` 改名成 `quarantined`，不动文档** | 2 项：`FAIL marker quarantine | 策略文档第 75 行声明了它，但 pytest.ini 里没有这个 marker`；`FAIL marker quarantined | pytest.ini 第 31 行声明了它，策略文档的 marker 表里没有归属——这个 marker 没人管` | 1 |
| **正常态** | `[gate-policy] 通过：策略文档、test.yml、pytest.ini 三者一致。` | 0 |
四次实验都用**先备份、后还原**做的，还原后 `git status --porcelain` 只剩本 handoff 文档自身的未提交改动、`restored-identical: True`。

> 注意 B 与 C 的方向是相反的、且都红，这是刻意的：脚本不是「只防放宽」，它防的是**两边说的不是同一件事**。
> 删掉 UI 豁免等于**加严**了门禁，但文档还写着「部分放行」，照样红——因为那时候文档在说谎。

---

## 四、每个测试层的最终结论

**marker 表**（`docs/质量门禁策略.md:65-75`）：

| marker | 归属层 | 谁在跑它 | 触发时机 | 阻塞性 | 条数 | 依据 |
|---|---|---|---|---|---|---|
| `api` | 宿主层（接口） | `pytest -m api -n 4 --dist loadfile` | PR / push main / 夜间 / 手动 | 阻塞 | 130 | 打公网 demo 接口；这一步同时挂 **40% 覆盖率门禁** |
| `contract` | 宿主层（契约） | `pytest -m contract -q` | 同上 | 阻塞 | 224 | 先跑 `export_openapi.py --check` 漂移门禁，再跑属性测试；样例数钉死 `CONTRACT_MAX_EXAMPLES=8` |
| `mes` | 宿主层（业务全链路） | `pytest -m mes` | 同上 | 阻塞 | 55 | fixture 自起本地 FastAPI + SQLite，不依赖外网，**CI 上最稳的一组**；刻意串行 |
| `login` | 宿主层（登录契约） | `pytest -m login` | 同上 | 阻塞 | 29 | 自起本地服务；绿＝「行为没有变化」，不等于「服务没问题」 |
| `ui` | 宿主层（浏览器） | `pytest -m ui` | 同上 | **不阻塞** | 14 | 打公网 `saucedemo.com`；理由/风险/退出条件见本 handoff 第七节（`docs/质量门禁策略.md:176-228`） |
| `smoke` | **横切标签** | 由 `api` / `mes` / `ui` 三步顺带跑到 | 同上 | 随宿主层 | 36 | **CI 里没有 `-m smoke` 这一步**，它是给人用的快速选择器 |
| `regression` | **横切标签** | 同上 | 同上 | 随宿主层 | 64 | **CI 里没有 `-m regression` 这一步**，发版前本地跑 |
| `flaky` | **质量治理标签** | 不单独跑，经 `NETWORK_FLAKY` 别名挂在 `api` 层 33 条上 | 随宿主层 | 随宿主层 | 33 | 重跑只对第三方抖动指纹生效，见第五节 |
| `quarantine` | **质量治理标签** | 主门禁中 deselect；需要时手动 `pytest -m quarantine` | 手动 | **不参与门禁** | 0 | 不判为通过，只退出计数并逐条点名 |

**「smoke/regression/flaky 不单独开 step」的判据（实测，不是推断）**：子集探测
`smoke` / `regression` / `flaky` / `api` / `ui` / `mes` / `login ⊆ (api or mes or ui)` 全为 `True`，
outside = 0；`contract` 为 `False`（224 条全在宿主层之外，所以它必须有自己的 step）。
反证：`-m "api and ui"` → `no tests collected`。

**job 表**（`docs/质量门禁策略.md:36-41`，全部 6 个 job 在 4 种触发下都跑）：

| job | 阻塞性 | 预算 | 判定步骤 | 失败找谁 |
|---|---|---|---|---|
| `lint` | 阻塞 | 10 | `ruff check` + `ruff format --check` | `@lefan17` |
| `typecheck` | 阻塞 | 15 | `mypy` | `@lefan17` |
| `test` | **部分放行** | 45 | 契约漂移、契约属性、接口组、业务全链路、登录契约；**UI 那一步除外** | `@lefan17` |
| `security` | 不阻塞 | 15 | `pip-audit`（扫描锁定的依赖树） | `@lefan17` |
| `pre-commit` | 阻塞 | 15 | `pre-commit run --all-files` + 门禁策略校验 | `@lefan17` |
| `commit-convention` | 阻塞 | 10 | `scripts/check_commit_msg.py` 校验最近 30 条提交 | `@lefan17` |

「失败找谁」六行同值是事实而非偷懒：`CODEOWNERS` 第 1 行是 `* @lefan17`，`testcases/`、`scripts/`、
`.github/`、`requirements*.txt`、`pyproject.toml` 也全部指向同一人——**单人仓库里不需要按路径分流**；
等有第二个 owner 才需要拆行（`docs/质量门禁策略.md:48-50`）。

附加事实：PR 上的红**不自动开 issue**，夜间与 `push main` 上的红才开
（`test.yml:474-475`，`if: failure() && (event_name == 'schedule' || event_name == 'push')`）——
因为 PR 上的红有作者在现场，再开 issue 只是噪音；而 main/夜间的红没有人当场看见，必须落到 issue 才有人管。

---

## 五、flaky / quarantine 承接策略

### 5.1 `flaky`：重跑只对「第三方抖动」指纹生效（`docs/质量门禁策略.md:118-144`）

实测现状：`testcases/api/flaky_policy.py:45`

```python
NETWORK_FLAKY = pytest.mark.flaky(reruns=1, only_rerun=NETWORK_JITTER_PATTERNS)
```

`NETWORK_JITTER_PATTERNS`（`flaky_policy.py:22-37`）是 **6 条**正则：
`实际状态码: (?:429|5\d\d)`、`Max retries exceeded`、`ConnectionError`、`Timeout`、
`响应不是合法 JSON`、`接口响应过慢`。挂载点 4 个用例文件：
`testcases/api/test_post.py:10`、`test_response_time.py:8`、`test_user_create.py:9`、`test_user_list.py:7`（共 33 条）。

三条边界（写完这三条，重跑才不构成洗红工具）：

1. **`addopts` 里没有 `--reruns`**（`pytest.ini:11` 是 `-v -raR --strict-markers`，`R` 只让重跑段被打印）。全局重跑会把真回归一起吞掉——「失败了但重跑过了」在报告里就是一个绿灯。
2. **指纹只收第三方抖动**：字段不匹配 / Schema 校验失败 / 类型错误 / 存在重复**被刻意排除**（`flaky_policy.py:39-41`）。匹配对象是 `f"{type(exc).__name__}: {exc}"`，不匹配指纹时 pytest-rerunfailures 走 `_should_hard_fail_on_error`，**当场硬失败，一次都不重跑**。
3. **重跑必须留痕**：根 `conftest.py:154-162` 记重跑次数与终态，写 `reports/flaky-governance.json`；CI 上 `test.yml:340-342` 用 `flaky_report.py --markdown` **无条件**（`if: always()`）打进流水线摘要，并上传 `flaky-governance` artifact。「这次是重跑才过的」必须天天可见。

**维护约束（写进文档的硬规则）**：`flaky` 不靠手打 `@pytest.mark.flaky` 使用——
实测全仓 `pytest.mark.flaky` 只出现在 `flaky_policy.py:43,45` 两处（定义与别名），**没有一处裸用法**。
新增公网用例必须挂 `NETWORK_FLAKY` 别名：裸标记会拿到插件默认 `reruns=1` 而**没有** `only_rerun` 收窄，等于对任何失败都重跑。

> ⚠️ **对 workflow 摘要文案的订正**：`test.yml:417` 原文写 `pytest -m flaky 只对第三方抖动指纹重跑`，
> 这句与实测机制不符——**CI 里没有 `-m flaky` 这一步**，重跑生效靠的是 `NETWORK_FLAKY` + `-m api` 那一步。
> 已在新策略文档里按机制写准（第三节 marker 表把 `flaky` 的「谁在跑它」写成「不单独跑，经 `NETWORK_FLAKY` 挂在 api 层」）。
> **未改 `test.yml:417` 的摘要文案**：改它属于「订正注释」而非本任务的四项交付物，留作未验证项/后续小改（见第十节第 3 条）。

### 5.2 `quarantine`：退出门禁，但必须点名（`docs/质量门禁策略.md:146-174`）

实测现状（根 `conftest.py`）：

- `pytest_collection_modifyitems`（`conftest.py:126-144`）：**非** quarantine 运行里把带 `quarantine` 标记的用例从 items 移除，并调用标准钩子 `pytest_deselected`，让它们计入 pytest 的 "N deselected" 计数。**这不是判为通过**。
- `pytest_terminal_summary`（`conftest.py:201-213`）：终端摘要逐条打印清单。原始输出见第九节验收 4。
- 同一份清单写进 `reports/flaky-governance.json` 的 `quarantined` / `quarantined_count`（`conftest.py:165-186`），CI 上打进流水线摘要。

策略三条：

1. 进隔离区必须附「谁在什么时候之前处理」的记录（落在 handoff 或 issue）。隔离区是**延期**不是**赦免**——没有归期的隔离用例就是被删掉的用例，只是没人承认。
2. **主门禁里清单必须可见**，且已被元测试锁住：`testcases/api/test_flaky_governance_meta.py:123` 断言 `f"DESELECTED {QUARANTINE_PROBE}::test_probe_quarantined_known_failure" in proc.stdout`。谁把打印去掉，元测试红。
3. **不为 quarantine 单独开 CI step**（`test.yml:356-359` 的既有决定）：当前 0 条，`pytest -m quarantine` 空跑返回 exit=5，GitHub Actions 会把它当失败——「一件好事」会天天把流水线弄红。清单由 `flaky_report.py --markdown` 无条件打印，信息不会丢。**当隔离区长期非空时这条要重新评估**：那时它不再空跑，而是每天都该有人看见的一组已知红。

---

## 六、`test.yml` 里仅剩的两处豁免（逐条理由）

| job | 步骤 | 为什么放行 |
|---|---|---|
| `test` | `运行 UI 自动化（失败不阻断流水线）`（`test.yml:299`） | 见下文第七节（原文只有「外部因素多」，现已补足） |
| `security` | `pip-audit（扫描锁定的依赖树）`（`test.yml:546`） | 它查的是**公共漏洞库的当前状态**而不是这份代码的质量；设成阻塞等于让「上游今天披露了一个 CVE」变成「任何人的 PR 今天都过不去」，结果一定是有人把它删掉（`test.yml:505-518`） |

两处豁免都**不是「不管了」**：UI 照样产出 Allure 报告与失败截图（`test.yml:371-401`），
`security` 的红照样出现在 job 日志与摘要里。区别只在于它们不获得把整条流水线弄红的权力。

**加严是单向的**（`docs/质量门禁策略.md:111-114`）：本仓库不允许把任何现有 job 从阻塞改成非阻塞——
收益是「今天我不用处理这个红」，代价是把一个真问题永久变成静默失效。
要新增检查就新增 job；要放宽已有的必须先改文档第二、四节并在 PR 里写清退出条件，而脚本会红着提醒。

---

## 七、UI 不阻塞：理由、风险、退出条件（`docs/质量门禁策略.md:176-228`）

### 7.1 为什么现在不阻塞（原文只有一句「外部因素多」，现展开为三条）

1. **失败归因不可靠**：UI 打公网 `https://www.saucedemo.com`（`data/config.yaml`）。一次失败可能来自选择器过期、站点改版、跨境网络抖动、浏览器版本漂移。**一个无法在失败瞬间区分「谁的错」的信号，不该拥有阻塞权**——阻塞权的前提是「红灯＝这段代码坏了」，这个前提在公网 UI 上不成立。
2. **没有可复现环境**：`api` / `mes` / `login` / `contract` 四层要么打可控 demo 接口、要么 fixture 自起本地服务，红灯能本地复现；UI 不能。
3. **耗时与不稳定互相放大**：`test.yml:294-296` 记录 UI 刻意不开 `-n` 并行（只有 2 个测试文件、`-n 4` 只用上 2 个 worker，启动开销大于收益；浏览器并发会抬升公共 demo 站失败率）。给「越并行越不稳」的层配阻塞权，换来的是「PR 排队等一个看天吃饭的 job」。

### 7.2 风险（说清了才叫权衡）

1. **静默腐烂**：最危险的后果不是「这次红了没人管」，而是「它已经红了三个月没人知道」。站点改一次 DOM → 三个选择器失效 → 红灯被豁免吞掉 → 下一个 PR 照样合。
2. **覆盖停止生长**：没有红灯压力就没人愿意往 UI 层加用例。UI 是**唯一**验证「用户可见行为」的层，它萎缩之后 `api` + `mes` 全绿也不代表用户能登录。
3. **报告靠自觉**：Allure 报告、`ui-screenshots` artifact（`test.yml:394-401`）、摘要都在产出，但**没有任何机制强制人打开**。

### 7.3 退出条件（满足任一条就应改成阻塞）

- **A. 外部依赖消失**：UI 不再打公网站点（改为 fixture 自起本地站点或全 mock 路由），失败可在本地复现。**一满足就立即改**，因为它直接消掉了 7.1 的第 1、2 条。
- **B. 稳定性有数据**：最近 **20 次** CI 运行的 UI step 中，「非代码原因」失败 ≤ 1 次（成功率 ≥ 95%），且每次失败都能凭截图/Allure 明确归因到站点或网络。→ 这也意味着**现在就该开始统计**，`reports/flaky-governance.json` 已是现成落点。
- **C. 站点改版与脚本陈旧解耦**：引入页面对象契约（对关键页面结构做快照断言），让「站点改版」表现为一条**确定的**契约失败而不是随机选择器超时。

改成阻塞时要同步改的四处（脚本会拦）：`test.yml:299`、本文档第二/四节、`README.md:501-529`、`CONTRIBUTING.md:128-132`、`CHANGELOG.md` 未发布段。

---

## 八、整合清单（据此合进原仓库）

按提交顺序；`0e61618` 是基线。合并时**不要** squash 成一条——6 条提交各自独立可读。

| # | commit | 文件 | 改动 | 冲突风险 |
|---|---|---|---|---|
| 1 | `7b6a044` `docs: 新增分层质量门禁策略文档` | `docs/质量门禁策略.md` | **新增 267 行**。三张机器可读表夹在 `<!-- gate-policy:jobs -->` / `:continue-on-error` / `:markers` 注释之间 | 无（新文件） |
| 2 | `117e31d` `feat(scripts): 新增门禁策略一致性校验脚本` | `scripts/check_gate_policy.py` | **新增 376 行**，纯标准库 | 无（新文件） |
| 3 | `2f1a30d` `ci: pre-commit job 接入门禁策略校验` | `.github/workflows/test.yml` | `+8`，在 `pre-commit run --all-files`（原 `test.yml:607`）之后插一步 | 低：只加 step，不动任何已有 step |
| 4 | `697ece6` `build(pytest): 补 marker 归属注释` | `pytest.ini` | `+5` 行注释，插在 `markers =` 之前 | 低：marker 名单与 `addopts` 未动 |
| 5 | `cef2e62` `docs: README 与 CONTRIBUTING 收敛到策略文档` | `README.md`(+14/-2)、`CONTRIBUTING.md`(+5/-2) | README：`501-504` 新增指向段、`512` pre-commit 行补一步、`526-529` UI 段展开、`136-139` flaky 段补指向；CONTRIBUTING：`128-132` 改为以策略文档为准 | 中：若原仓库 README 行号已漂移，按内容锚点找（「共 **6 个 job**」「UI 用例标了 `continue-on-error`」） |
| 6 | `d024a42` `docs: CHANGELOG 记录波 3 门禁策略显式化` | `CHANGELOG.md` | `+32`，插在 `## [未发布]`（原 `CHANGELOG.md:13`）之后、`### 新增 —— 认证层与契约驱动（波 1）`（`47`）之前 | 中：同段位若已有其他波次条目，按插入位置对齐 |

`git diff --stat 0e61618..HEAD`：

```
 .github/workflows/test.yml                         |   8 +
 CHANGELOG.md                                       |  32 ++
 CONTRIBUTING.md                                    |   7 +-
 README.md                                          |  16 +-
 "docs/质量门禁策略.md"                              | 267 +++++++++++++++
 pytest.ini                                         |   5 +
 scripts/check_gate_policy.py                        | 376 +++++++++++++++++++++
 7 files changed, 707 insertions(+), 4 deletions(-)
```

**是否有意保持「没有任何现有 job 从阻塞改为非阻塞」：是，刻意保持。**
`git diff 0e61618..HEAD -- .github/workflows/test.yml` 的全部内容是 8 行新增（一段注释 + 一个新 step），
`continue-on-error` 出现次数改动前后都是 2（`test.yml:299`、`546`），**没有任何一处的值被改过**。

---

## 九、自检命令与原始输出

```powershell
$py = 'C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
cd C:\Users\lefan\DSH_project\接口自动化_3\work\w3-gate
```

| 命令 | 关键输出 | 退出码 |
|---|---|---|
| `python scripts/check_gate_policy.py` | `[gate-policy] 通过：策略文档、test.yml、pytest.ini 三者一致。` | **0** |
| `python -m ruff check .` | `All checks passed!` | **0** |
| `python -m ruff format --check .` | `96 files already formatted` | **0** |
| `python -m mypy` | `Success: no issues found in 75 source files` | **0** |
| `python scripts/check_commit_msg.py .git/COMMIT_EDITMSG`（对 6 条提交逐条） | 6 条全部按中文 conventional commit 写入，均通过 | 0 |
| `pytest --markers -q -o addopts=`（正则计数 9 个 marker 行） | 9 个 marker 仍全部被 pytest 认到 | — |

校验脚本正常态的完整输出（`python scripts/check_gate_policy.py`，节选）：

```
[gate-policy] workflow job：lint、typecheck、test、security、pre-commit、commit-convention
[gate-policy] pytest.ini marker（9 个）：smoke、regression、api、ui、mes、login、contract、flaky、quarantine

== ① job 集合 ==
  PASS  job commit-convention | 两边都有（workflow:626 / 文档:41）
  ...
== ② 阻塞性与超时 ==
  PASS  lint 阻塞性 | 文档「阻塞」→ workflow 放行步骤 0 个
  PASS  security 阻塞性 | 文档「不阻塞」→ workflow 放行步骤 1 个
  PASS  test 阻塞性 | 文档「部分放行」→ workflow 放行步骤 1 个
== ③ continue-on-error 放行清单 ==
  PASS  security / pip-audit（扫描锁定的依赖树） | true（workflow:546 / 文档:104）
  PASS  test / 运行 UI 自动化（失败不阻断流水线） | true（workflow:299 / 文档:103）
== ④ marker 归属 ==
  PASS  marker 覆盖 | 9 个 marker 全部有归属，且没有多余条目
```

> 注：job 行号在脚本输出里是 **626 / 35 / 76 / 107 / 519 / 574**（`test.yml` 加了一步后 `commit-convention` 由 618 变 626），
> 文档行号是 `36-41`。两边都对得上。

**验收标准 1（与 `test.yml` 逐条对照）**见上文第四节的 marker 表与 job 表；对照命令即 `python scripts/check_gate_policy.py`，
它输出的每一行 `PASS` 都带两边行号，就是那张对照表本身。

**验收标准 2（反向验证）**：见第三节 A / B / C / D 四个实验，全部 `exit=1`，实验后均还原（`restored-identical: True`，`git status --porcelain` 为空）。

**验收标准 3（正常态绿）**：`exit=0`，见上。

**验收标准 4（quarantine 可见性 + flaky 指纹现状）**：

```powershell
python -m pytest testcases/api/_probes/probe_quarantine.py testcases/api/_probes/probe_flaky_rerun.py -q --no-header
```

原始输出关键行：

```
collected 2 items / 1 deselected / 1 selected
testcases\api\_probes\probe_flaky_rerun.py R.                            [100%]
=========================== quarantine：1 条用例已退出主门禁 ============================
  DESELECTED testcases/api/_probes/probe_quarantine.py::test_probe_quarantined_known_failure
这些用例既没有被删除，也没有被判为通过——它们的红不再计入本次门禁。
单独运行：pytest -m quarantine
账本（供周报统计）：C:\Users\lefan\DSH_project\接口自动化_3\work\w3-gate\reports\flaky-governance.json
=========================== rerun test summary info ===========================
RERUN testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time
PASSED testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time
============= 1 passed, 1 deselected, 1 warning, 1 rerun in 0.24s ==============
```

→ **实测结论**：隔离区清单在主门禁运行时的终端摘要里确实逐条可见（`DESELECTED <nodeid>`），
同时写入账本；`flaky` 的 `only_rerun` 指纹现状是 6 条正则、经 `NETWORK_FLAKY` 挂在 33 条公网用例上，重跑段在 `-raR` 的 `R` 下可见。

---

## 十、未验证项清单

| # | 项 | 为什么没验证 | 影响 |
|---|---|---|---|
| 1 | `pre-commit job` 里新增那一步在 GitHub runner 上的实际执行 | 本机装不上 pre-commit（`ensurepip` 被拒 `PermissionError [WinError 5]`，`pip install pre-commit` 超 300s 无返回，见 `.pre-commit-config.yaml` 文件头），且本任务禁止 push。**只验证了 `python scripts/check_gate_policy.py` 这一步本身在本地绿** | 低。新 step 独立于 pre-commit 本体，只依赖 `python` 与仓库内文件；`ruff`/`mypy` 在本机均已实测通过 |
| 2 | 新增 step 在 CI 上的耗时 | 无法在本地跑 runner | 低。脚本纯文本比对，本机 `< 0.2s` |
| 3 | `test.yml:417` 摘要文案「`pytest -m flaky` 只对第三方抖动指纹重跑」与该机制不符 | 属 workflow 注释订正，不属本任务四项交付物；改动 `.github/workflows/` 虽在可动边界内，但会与「只加严」的表述混在一起，故留作单独小改 | 低但**真实**：CI 摘要里那句话会让读者以为有个 `-m flaky` 的 step。已在 `docs/质量门禁策略.md` 按机制写准，建议后续单独一条 `docs(ci):` 提交订正 |
| 4 | 退出条件 B「近 20 次 CI 的 UI step 非代码原因失败 ≤ 1 次」的**当前实际值** | 需要历史 CI 运行数据，本机拿不到（无 push 权限、无 Actions 历史） | 该条件目前只是**判据**而不是现状陈述。文档里已写明「这也意味着现在就该开始统计」 |
| 5 | UI 退出条件 A 提及的「改为 fixture 自起本地站点」可行性 | 未做原型（超出本任务边界：不能改 `testcases/**`） | 低。它是**未来**触发时要做的判断，不是当下的交付 |
| 6 | 文档第六节引用的 `test.yml:294-296` / `356-359` / `505-518` 等行号 | 这些行号在加一步之后（`+8` 行在 610 行处，位于它们之后）**未变**，已复核；但若原仓库 `test.yml` 已漂移，行号需重核 | 低。校验脚本不依赖这些散文行号，只依赖三张结构化表 |
| 7 | `README.md` 里的「6 个 job」计数 | **未改**（本任务没有新增 job，只新增了一个 step），因此不需要改 | 无 |

---

## 十一、实测 / 推断 / 未验证 的分界

- **实测**：9 个 marker 与用例数（`--collect-only`）、6 个 job 的 timeout 与 2 处 `continue-on-error`（缩进扫描 + 独立探针交叉验证）、marker 子集关系（outside = 0）、隔离区清单可见性（原始 pytest 输出）、flaky 指纹 6 条正则与 4 个挂载文件、`ruff`/`mypy`/`check_gate_policy.py` 的退出码、三次反向验证实验的红证与还原。
- **推断**：`smoke`/`regression` 作为「选择器」而非「层」的价值判断；UI 层「覆盖停止生长」的风险；`pre-commit` job 里跑这一步在 CI 上仍是秒级。
- **未验证**：第十节全部 7 条（CI 实跑、退出条件 B 的历史数据、`test.yml:417` 文案订正、原仓库行号漂移风险）。
