# 波 3 · P2-1 对接测试管理平台（禅道）— 交接文档

> 基线：`0e61618`（波 2 收口）→ HEAD（4 条提交，纯新增 2017 行，见 §7）。
> 全部实测在 Windows 11 + Python 3.12.10 + pytest 8.3.3 上完成，解释器
> `C:\Users\lefan\DSH_project\DH_1\fullstack-test-framework\.venv\Scripts\python.exe`。
> 本文里每个数字都附了产生它的原始命令；**实测 / 推断 / 未验证**三类已逐处标注。

本轮的定位是**把已经在代码里、却只活在人眼里的「用例编号」变成一条可机读的链路**，
而不是给仓库新增一套测试编号制度。原始定义（波划分原文）：

> 对接测试管理平台：`test_login_contract.py` 已经埋了 `LC-14`~`LC-23` 这种用例编号，
> `contract.py:24` 也是按编号组织的——接 TestRail / 禅道做双向追溯只差一个上报脚本。

用户已明确选择**禅道（ZenTao）**。

---

## 1. 现状 → 做了什么

### 1.1 现状（实测）

| 事实 | 证据 |
|---|---|
| `testcases/login/test_login_contract.py` 里 LC-01~LC-30 齐备，但编号只在 docstring 首行与 parametrize 的 `ids` 字符串里 | 见 §1.3 |
| 全仓无任何 JUnit-XML 上报、无 TestRail / 禅道痕迹 | `git grep -il "zentao\|testrail\|junit"` 仅命中 `scripts/flaky_report.py` 的注释 |
| 已有机读产物只有 flaky 账本 | `C:\...\w3-zentao\conftest.py:109` 的 `_FLAKY_SIDECAR` 写 `reports/flaky-governance.json` |
| **154/181 个 nodeid 没有编号** | `python scripts/case_index.py` → `无编号的 nodeid: 154` |

### 1.2 新增三个文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `scripts/case_index.py` | 532 | 扫描 `testcases/`，产出「用例编号 → 文件 / 函数 / nodeid / 套件」映射到 `reports/case-index.json` |
| `scripts/report_to_zentao.py` | 453 | 把 pytest 运行结果按用例编号上报禅道；**默认 dry-run**，真实上报要 `--send` + 环境变量凭据 |
| `scripts/test_report_to_zentao_stub.py` | 431 | 本地 stdlib 桩 HTTP 禅道，逐字段断言请求形状；9 条测试 |

### 1.3 核心难点：编号出现在两种位置

这是本任务真正的技术含量所在，也是**不能只靠 docstring 抽编号**的原因。

**来源一 · 函数 docstring 首行**（`testcases/login/test_login_contract.py:56`）：

```python
def test_lc01_login_success(login_api):
    """LC-01 正确用户名 + 正确密码 -> 200，返回 token 与 user。"""
```

**来源二 · `parametrize` 的 `ids` 字符串**（同文件 `:108-121`），LC-06~LC-13 八条挤在一个函数里：

```python
@pytest.mark.parametrize(
    ("payload", "case"),
    [
        ({}, "LC-06 缺全部字段"),
        ...
        ({"password": 0}, "LC-13 密码为数字 0（falsy 被当成没传）"),
    ],
)
def test_lc06_to_lc13_missing_fields_400(login_api, payload, case):
```

采集代码在 `scripts/case_index.py:200` `_scan_function()`：

```python
func.docstring_ids, func.inline_refs = _leading_ids(first_line)  # :203 来源一
func.has_params = _parametrize_has_values(node)  # :204
```

参数行的编号由 `scripts/case_index.py:172` `_iter_param_strings()` 递归取（含嵌套元组，深度上限 4），
`scripts/case_index.py:118` `_id_spans()` 负责把 `LC-06~LC-13` 这种**区间展开**：

```python
def _expand_range(prefix: str, start: str, end_prefix: str | None, end: str) -> list[str]:  # :108
    if end_prefix is not None and end_prefix != prefix:
        return []  # 跨前缀不是区间，宁可漏也不编。
    lo, hi = int(start), int(end)
    if hi < lo or hi - lo >= MAX_RANGE_SPAN:
        return []
    width = max(len(start), len(end))
    return [f"{prefix}-{n:0{width}d}" for n in range(lo, hi + 1)]
```

### 1.4 三个关键设计决策（含为什么）

**① 两源 join，而不是纯静态预测 nodeid。**
先用 AST 抽编号，再跑真实的 `pytest --collect-only -q` 取 nodeid，在 `(模块, 限定函数名)` 上 join
（`scripts/case_index.py:265` `collect_nodeids()`、`:301` `_resolve()`）。

理由：本仓库同时存在三种写法，纯静态预测 nodeid **在三种上全错**——

- `testcases/api/test_user_list.py:31` `ids=lambda v: f"user-{v}"`：`ids` 是可调用对象，AST 只能得 `1`，pytest 出 `user-1`；
- 类内测试（`testcases/mes/test_mes_business.py:71` 等 6 个类）：nodeid 多一层 `::TestIdempotency`；
- `testcases/contract/test_contract_export.py:124` parametrize 的参数值是模块级名字 `UNDECLARED_BUT_REACHABLE`，AST 里不是字面量。

首版纯 AST 得 184 个预测 nodeid，与实测 181 对不上（只对上 109 个）；改两源后完全自洽。
**代价**：`--static-only` 档只能给编号、nodeid 留 `null`，绝不编造（`:342` `build_index()` 的 `static_only` 分支）。

**② 编号的「形状」分不出用例还是缺陷，所以前缀走显式登记表**（`scripts/case_index.py:75`）：

```python
CASE_ID_SCHEMES = {
    "LC": "登录契约用例（docs/login-test-cases.md，30 行表格）",
}
NON_CASE_ID_SCHEMES = {
    "DEF": "契约缺陷（docs/wave1-contract-handoff.md「真实缺陷清单」）",
    "FIND": "登录缺陷（testcases/login/test_login_contract.py 内联标记）",
}
```

理由：`LC-01` 与 `DEF-03` 语法完全一样，区别只在意图。`DEF-01`~`DEF-06` 是
`docs/wave1-contract-handoff.md`「真实缺陷清单」里的**契约缺陷**；`FIND-01`~`FIND-06` 是登录缺陷内联标记。
若把形状当判据，索引会凭空多出十几条不存在的「用例」。

**③ 只认行首 / 串首编号，行内编号记为「引用」。**
理由：行内 `【FIND-04】` 这类若算用例编号，索引同样会凭空多出 12 条。实测区分结果：
`仅作文本引用、不算用例编号的前缀: DEF: 1, FIND: 5`；`行首出现但前缀未登记为用例编号（缺陷号等）: DEF: 3`。

**④ 参数行编号优先于 docstring 区间**（`scripts/case_index.py:301` `_resolve()`，`:317-318`）：

```python
token_hits = [h for h in hits if h.nodeid_token is not None]
chosen = token_hits or hits
```

理由：`test_lc06_to_lc13_missing_fields_400` 的 docstring 写的是区间 `LC-06~LC-13`，参数行也各带编号。
若让 docstring 区间回退到函数级，8 条编号会被放大成 8×8=64 条假映射。

### 1.5 上报脚本的设计

**输入为什么不用 JUnit XML 单独承载。**
`scripts/flaky_report.py` 的 docstring 已经写明这条既有约束：

> 为什么不用 --junit-xml 当载体：JUnit 的 XML 格式里**没有** rerun 这个概念
> （venv 里 `_pytest/junitxml.py` 的 `pytest_runtest_logreport` 只认 passed/failed/skipped）

所以输入是**两路 join**：

| 输入 | 来源 | 提供什么 |
|---|---|---|
| `--junit-xml` | `pytest --junit-xml=` | nodeid → 结局（passed/failed/error/skipped） |
| `--ledger` | 根 `conftest.py:109` 写的 `reports/flaky-governance.json` | nodeid → 重跑次数（`rerun_passed` / `rerun_still_failed`） |
| `--case-index` | `scripts/case_index.py` 产物 | 用例编号 → nodeid |
| `--case-map` | `docs/zentao-case-map.json` | 用例编号 → 禅道用例 ID |

重跑次数**只能写进禅道的 `real` 字段**，不能写进 `result`——禅道的 `result` 是四值枚举
（`n/a|fail|blocked|pass`），多塞一维就是伪造（`scripts/report_to_zentao.py:316` `_describe()`）：

```python
bits = [f"fullstack-test-framework 自动上报：{outcome.outcome}"]
if reruns:
    bits.append(f"重跑 {reruns} 次（JUnit XML 不表达 rerun，此数取自 reports/flaky-governance.json）")
```

**payload 构造那几行**（`scripts/report_to_zentao.py:136-142`）：

```python
@property
def path(self) -> str:
    return RESULTS_PATH.format(case_id=self.zentao_case_id)  # :138


@property
def body(self) -> dict[str, Any]:
    return {"steps": [{"result": self.result, "real": self.real}]}  # :142
```

`RESULTS_PATH` 定义在 `scripts/report_to_zentao.py:63`：

```python
RESULTS_PATH = "/api.php/v1/testcases/{case_id}/results"
```

**凭据只从环境变量读**（`scripts/report_to_zentao.py:156-161`，全部 6 行）：

```python
def read_credentials(env: Mapping[str, str]) -> Credentials:
    """凭据只从环境变量读，一处读、一处存，别的地方不许再碰 os.environ。"""
    missing = [name for name in (ENV_URL, ENV_TOKEN) if not env.get(name, "").strip()]
    if missing:
        raise MissingCredentials(missing)
    return Credentials(url=env[ENV_URL].strip().rstrip("/"), token=env[ENV_TOKEN].strip())
```

变量名在 `:79-81`：`ENV_URL = "ZENTAO_URL"` / `ENV_TOKEN = "ZENTAO_TOKEN"` / `ENV_PRODUCT = "ZENTAO_PRODUCT"`。
**命令行没有凭据参数**——避免进 shell 历史与进程表。认证头**只在发送那一层出现**（`:342`），
`PlannedRequest` 结构里根本没有 token 字段，所以 payload 产物天然不含凭据。

**禅道用例 ID 的映射表为什么入库。**
`reports/` 被 `.gitignore:10` 忽略（`git check-ignore -v reports/case-index.json` →
`.gitignore:10:reports/`），所以 case-index 与 payload 都是**产物**，不入仓。
但「LC-01 → 禅道用例 101」是**人在禅道建完用例后手填的资产**，禅道自己分配 ID，任何脚本都推不出来——
它是配置不是产物，因此落在 `docs/zentao-case-map.json`（`git check-ignore` 未命中，可入库）。
当前该文件 `cases` 为空对象 `{}`，`_testtask: 0`——**这正是当前此刻的一致状态**：还没有真实禅道实例，
所以一个 ID 都没登记。空表不会让脚本静默跳过，30 条 LC 编号会原样列进 `skipped`（见 §4.3）。

---

## 2. 禅道 API 端点清单（逐条标注置信度）

抓取方式：`Invoke-WebRequest "https://www.zentao.net/book/api/<id>.html?fullScreen=..."` 取正文去标签。
手册为**禅道二次开发手册**（`www.zentao.net/book/api/`）。

| # | 端点 | 置信度 | 依据 / 备注 |
|---|---|---|---|
| Z1 | `POST {url}/api.php/v1/tokens`；body `{"account","password"}` → `{"token":...}` | **公开文档确认** | 手册页 1397。curl 原文给出 `-H "Content-Type: application/json"` |
| Z2 | 除取 token 外，鉴权 header 名为 **`Token`**（大写 T） | **公开文档确认** | 手册页 962 请求头表：`Token | String | 是 | 认证凭证Token`；curl 示例 `-H "Token: ..."` |
| Z3 | `POST {url}/api.php/v1/testcases/{id}/results` — **执行用例（本脚本用的就是它）** | **公开文档确认** | 手册页 962，2024-09-30 最后编辑。见下方原文摘录 |
| Z4 | Z3 的请求体 `steps`（array，必填），元素 `{"result","real"}`，`result ∈ n/a\|fail\|blocked\|pass` | **公开文档确认** | 页 962 请求示例原文：`{"steps":[{"result":"n/a","real":"忽略"},{"result":"fail","real":"失败"},{"result":"blocked","real":"阻塞"},{"result":"pass","real":"成功"}]}` |
| Z5 | Z3 的查询参数 `testtask`（int，否，默认 0）、`version`（int，否，默认最新版本） | **公开文档确认** | 页 962 请求参数表 |
| Z6 | `POST {url}/api.php/v1/products/{id}/testcases` — 创建用例 | **公开文档确认** | 手册页 726。body：`branch`/`module`/`story`（否）、`title`（是）、`type`（是）、`steps`（是，元素 `{"desc","expect"}`）、`files`、`currentVersion` |
| Z7 | `GET {url}/api.php/v1/products/{id}/testcases` — 产品用例列表 | **公开文档确认** | 手册页 725。响应 `page/total/limit/testcases[]`，元素含 `id`（int） |
| Z8 | `POST {url}/api.php/v1/products/{id}/bugs` — 创建 Bug | **公开文档确认** | 手册页 721。body：`branch`/`module`/`execution`（否）、`title`（是）、`keywords`（否）、`severity`（是 int）、`pri`（是 int）、`type`（是 string） |
| Z9 | `GET {url}/api.php/v1/testtasks` — 测试单列表 | **公开文档确认** | 手册页 730 |
| Z10 | v2 系列：`POST /api.php/v2/testcases`（2201）、`GET /api.php/v2/testcases/:caseID`（2206）、`POST /api.php/v2/bugs`（2192）、`POST /api.php/v2/testtasks`（2234）、`POST /api.php/v2/users/login`（2142） | **公开文档确认** | 均为 v2 slug 页正文实见；**注意 v2 的鉴权 header 名是小写 `token`，且 v2 取 token 走 `/users/login`（与 v1 的 `/tokens` 不是同一个）** |
| Z11 | v2 的「执行用例结果」端点（若形如 `POST /api.php/v2/testcases/:id/results`） | **推断（未在公开手册页正文中找到）** | 需真实实例核实。因此本脚本**只用 v1 的 Z3** |
| Z12 | `testtask` 是否必须非 0 才能落库；`version` 是否必填 | **需真实实例核实** | 已归档的禅道论坛帖曾报「开源版调用 `/testcases/id/results` 不起作用」与「传了 testTask 的 Id 但没更新测试单中用例结果」 |
| Z13 | `Token` header 大小写是否真的不敏感（网关层） | **需真实实例核实** | HTTP 规范说大小写不敏感，但服务端网关未必；脚本按 v1 文档发大写 `Token` |
| Z14 | v1 RESTful API 的版本门槛（手册提到 16.5+，但本次抓到的页面正文未复现该结论） | **需真实实例核实** | 见 §6 未验证项 |
| Z15 | 「LC-01」这类用例编号在禅道侧由谁承载（`title` 前缀？还是不加） | **需真实实例核实** | 当前 `docs/zentao-case-map.json` 只登记 ID，不约束禅道侧写法 |

### 手册页 962 原文摘录（拼接自 HTML 正文，顺序保留）

```
POST
/testcases/id/results
执行用例
请求URL        https://xxx.com/api.php/v1/testcases/id/results
请求头         Token  String  是  认证凭证Token
请求参数       testtask  int  否  测试单的ID，默认为0。用于标识测试用例在测试单中执行
               version   int  否  测试用例版本，默认为测试用例的最新版本
请求体         steps     array 是  用例结果和描述，结果可选"n/a|fail|blocked|pass"
请求示例       { "steps": [ {"result":"n/a","real":"忽略"}, {"result":"fail","real":"失败"},
                            {"result":"blocked","real":"阻塞"}, {"result":"pass","real":"成功"} ] }
请求响应       名称 类型 必填 描述        message  string  是  失败信息，若成功则返回空数组
```

---

## 3. 双向追溯链路（哪一环只能人工）

```
① 用例编号            LC-01 … LC-30
      │                     ▲ 人写（已存在，本轮只把它读出来）
      │  scripts/case_index.py（AST + 真实 collect-only 两源 join）
      ▼
② nodeid              testcases/login/test_login_contract.py::test_lc01_login_success
      │                     ▲ 机器
      │  pytest --junit-xml=  +  reports/flaky-governance.json
      ▼
③ 执行结果             passed / failed / error / skipped  +  重跑次数
      │                     ▲ 机器
      │  docs/zentao-case-map.json  ← ★★★ 唯一的人工环 ★★★
      ▼
④ 禅道用例 ID          101  →  POST /api.php/v1/testcases/101/results
      │                     ▲ 机器（scripts/report_to_zentao.py）
      ▼
⑤ 禅道侧的用例执行记录 / 测试单结果
      │                     ▲ 机器（禅道自己）
      ▼
⑥ 缺陷（Bug）          POST /api.php/v1/products/{id}/bugs
                            ▲ 人工判定 + 人工触发（见下）
```

### 当前只能人工的两环

1. **③→④：用例编号 → 禅道用例 ID 的映射**。
   禅道用例 ID 由禅道在创建用例时分配，任何脚本都推不出来。
   当前 `docs/zentao-case-map.json` 的 `cases` 是空对象，所以脚本**一条都不发**，
   而是把 30 条编号连名字带原因列进 `skipped`（§4.3）。
   人要做的事：在禅道建好用例 → 把 ID 填进该文件的 `cases`。
   填错形状脚本会以 `SystemExit` 拒绝，不会静默忽略（`scripts/report_to_zentao.py:262`）。
2. **⑤→⑥：执行失败 → 缺陷**。脚本**不自动开 Bug**。理由：一条自动化断言失败
   不等于一个可交付的缺陷（可能正是被测服务按契约拒绝非法输入）。自动开单会把禅道淹成噪声，
   而「禅道里的 Bug 都是真的」这条信任一旦丢掉就回不来。因此 Z8 端点只列在清单里，未接。

---

## 4. 验收证据（原始命令 + 输出关键行）

### 4.1 验收标准 1：case_index 产出 + 有/无编号准确计数 + 三方对账

```
> python scripts/case_index.py
模式: collect    扫描目录: testcases
测试函数: 148
收集到 nodeid: 181
有编号的 nodeid: 27
无编号的 nodeid: 154   <-- 覆盖缺口，不掩饰
编号条目: 30（已解析 30 / 未解析 0）
其中只定位到函数、定位不到参数行的: 0
去重编号: 30    带编号的函数: 17

已登记为用例编号的前缀: LC
已登记为**非**用例编号的前缀: DEF、FIND
编号前缀分布（去重后）:
  LC: 30

无编号 nodeid 按套件:
  mes: 55
  api: 47
  contract: 36
  ui: 14
  login: 2

一条 nodeid 承载多个编号（缺陷定位精度到此为止）:
  testcases/login/test_login_contract.py::test_lc22_to_lc25_decision_table  ->  LC-22、LC-23、LC-24、LC-25

仅作文本引用、不算用例编号的前缀:
  DEF: 1
  FIND: 5

行首出现但前缀未登记为用例编号（缺陷号等）:
  DEF: 3

收集命令退出码 1；<venv python> -m pytest <abs>/testcases --collect-only -q -p no:cacheprovider -o addopts= --continue-on-collection-errors

索引已写入: C:\Users\lefan\DSH_project\接口自动化_3\work\w3-zentao\reports\case-index.json
EXIT=0
```

`reports/case-index.json` 的 `totals`（机器可读）：

```json
{"test_functions":148,"nodeids_collected":181,"nodeids_with_case_id":27,
 "nodeids_without_case_id":154,"case_id_entries":30,"case_id_resolved":30,
 "case_id_unresolved":0,"case_id_function_level_only":0,
 "distinct_case_ids":30,"functions_with_case_id":17}
```

**独立核对（不与 case_index 共享代码）**：

```
> python -m pytest testcases/login --collect-only -q -p no:cacheprovider
29 tests collected in 0.04s

> (Select-String -Path docs/login-test-cases.md -Pattern '^\|\s*LC-').Count
30
```

**三个数字为什么不同（不掩盖）**：

| 数字 | 来源 | 口径 |
|---|---|---|
| **30** | `docs/login-test-cases.md` 表格行 / 去重编号 | 设计用例数 |
| **29** | `pytest -m login --collect-only` | login 目录可收集 nodeid 数 |
| **27** | `case-index.json` 里 `suite=login` 且有 nodeid 的行 | 编号成功落到 nodeid 的条数 |

差异全部有解释，不是漏抽：

- **29 = 27 + 2**：login 目录有 2 个 nodeid **本来就没有编号**——
  `testcases/login/test_login_contract.py::test_health_endpoint_matches_contract` 与
  `testcases/login/test_login_contract.py::test_undocumented_status_codes_are_still_undocumented`。
- **30 = 27 + 3**：LC-22~LC-25 四条编号挤在**同一个** nodeid
  `testcases/login/test_login_contract.py::test_lc22_to_lc25_decision_table` 里——
  该函数的四个 payload 写在函数体内的 `cases` 列表里用 `for` 循环跑（`testcases/login/test_login_contract.py:210-225`），
  **没有 parametrize**，所以 pytest 只收集到 1 个 nodeid。产物里以
  `one_nodeid_multiple_case_ids` 显式登记，不假装它们各有 nodeid。
- 另有一处**反向**抵消：LC-06~LC-13 的 docstring 写的是区间 `LC-06~LC-13`，但参数行各带编号，
  于是展开成 8 个 nodeid（`resolution=param-token`）。

`resolution` 分布实测：`base 18 / param-token 12 / function-level 0`（`function-level` 为 0 说明没有一条编号只能定位到函数）。

### 4.2 验收标准 2：桩服务两条路径 + 反向验证

```
> python -m pytest scripts/test_report_to_zentao_stub.py -q -p no:cacheprovider -o addopts=
.........                                                                [100%]
9 passed in 8.08s
exit=0
```

9 条测试的覆盖（`scripts/test_report_to_zentao_stub.py`）：

| 行 | 测试 | 覆盖 |
|---|---|---|
| :263 | `test_dry_run_sends_nothing_even_when_stub_is_closed` | dry-run 时**桩已关闭**仍退出 0、桩上零记录 |
| :287 | `test_dry_run_flag_is_accepted_and_conflicts_with_send` | 显式 `--dry-run` 可用；`--dry-run --send` 互斥 |
| :303 | `test_dry_run_payload_shape` | 产物形状：`{steps:[{result,real}]}`，多一个字段都算猜测 |
| :330 | `test_dry_run_lists_unmapped_case_code_by_name` | 未登记编号必须**有名字地**跳过；`--strict` 升为 exit 4 |
| :344 | `test_dry_run_reports_case_that_did_not_run` | 本次没跑到的编号同属 skipped |
| :365 | `test_send_posts_expected_requests` | **真发**，逐字段断言 |
| :396 | `test_send_without_credentials_refuses_and_sends_nothing` | 缺凭据拒绝且一条都不发 |
| :406 | `test_send_aborts_on_5xx_and_tolerate_errors_finishes` | 500 分支与 `--tolerate-errors` |
| :423 | `test_send_cannot_reach_stub_is_a_failure_not_a_pass` | 连不上桩必须是失败，不许当通过 |

**真实发送路径的断言原文**（`scripts/test_report_to_zentao_stub.py:373-378`）：

```python
assert first.method == "POST"
assert first.path == EXPECTED_PATH_101  # "/api.php/v1/testcases/101/results"
assert first.query == {"testtask": ["77"]}
assert first.headers["token"] == FAKE_TOKEN
assert first.headers["content-type"] == "application/json"
assert first.json()["steps"][0]["result"] == "pass"
```

**反向验证（证明这些断言真的会红）**：把 `scripts/report_to_zentao.py:63` 的
`RESULTS_PATH` 从 `.../results` 临时改成 `.../RESULTZ`，重跑（篡改与还原由一次性脚本做，
避免留下手工改坏的源码）：

```
> python -m pytest scripts/test_report_to_zentao_stub.py -q -p no:cacheprovider -o addopts=
>       assert [r["path"] for r in artifact["requests"]] == [EXPECTED_PATH_101, EXPECTED_PATH_106]
E       AssertionError: assert ['/api.php/v1.../106/RESULTZ'] == ['/api.php/v1.../106/results']
scripts\test_report_to_zentao_stub.py:314: AssertionError
>       assert first.path == EXPECTED_PATH_101
```

受控复跑的完整结果：

```
=== 篡改后 ===
exit = 1
2 failed, 7 passed in 8.03s
=== 还原后 ===
exit = 0
9 passed in 8.02s
=== 还原确认 ===
RESULTS_PATH = "/api.php/v1/testcases/{case_id}/results"
```

**桩测试不是恒绿的门面**：改坏端点会让 9 条里的 2 条变红，还原即全绿，
且 `git status --short` 在还原后为空（篡改没有留在树里）。

### 4.3 验收标准 3：凭据不经环境变量就无法上报 + 产物里 grep 不到

```
> python scripts/report_to_zentao.py --send --junit-xml <...>\login.xml
缺少环境变量：ZENTAO_URL、ZENTAO_TOKEN
凭据只从环境变量读，命令行没有对应参数（避免进 shell 历史与进程表）。例如：
  $env:ZENTAO_URL='https://zentao.example.com'; $env:ZENTAO_TOKEN='<token>'
  python scripts/report_to_zentao.py --send
没有真实禅道实例时不要用 --send：默认的 dry-run 不需要任何凭据。
exit=3
```

（`exit=3` 是设计路径，见 `scripts/report_to_zentao.py:93` `MissingCredentials.code = 3`；
没设 junit 路径时是 exit=1 `输入文件不存在`，两条路径都实测过。）

**grep 凭据**：设 `ZENTAO_TOKEN='SUPER-SECRET-TOKEN-9c1f'`、`ZENTAO_URL='https://zentao.invalid.example'`，
跑 dry-run 并把 payload 落盘，再 grep 产物：

```
> Select-String -Path "<...>\*.json" -Pattern 'SUPER-SECRET-TOKEN-9c1f' -SimpleMatch
（无输出）
grep 命中数: 0
```

产物 `zentao-payload-withcred.json`（6512 字节，与不设凭据时**字节数完全相同**）里只有：

```json
"zentao_base_url": "(dry-run，未读凭据)",
"note": "认证头不在产物里；发送时用 ZENTAO_TOKEN 环境变量注入 Token 头。"
```

三重保险：① `PlannedRequest` 结构里没有 token 字段；② `Credentials.__repr__` 覆写成
`token=<已设置>`（`scripts/report_to_zentao.py:89-90`），有人 `print(credentials)` 也漏不出去；
③ 认证头只在 `post_request()` 内构造（`:342`），dry-run 根本走不到那一层。
桩测试里也有一条硬断言（`scripts/test_report_to_zentao_stub.py:326-327`）：
`assert FAKE_TOKEN not in text`，且请求字典里不许出现任何含 `token`/`auth` 的键。

### 4.4 验收标准 4：dry-run 默认不产生任何网络请求

```
> python scripts/report_to_zentao.py --junit-xml <...>\login.xml     # 默认档，无桩、无凭据
待发送 0 条请求 → (dry-run，未读凭据)（认证头：Token <不回显>）
跳过 30 条（不静默）：
  LC-01: 禅道用例 ID 未登记  nodeid=testcases/login/test_login_contract.py::test_lc01_login_success
  ...
exit=0

> python scripts/report_to_zentao.py --dry-run --junit-xml <...>\login.xml      # 显式写出这层意图
待发送 0 条请求 → (dry-run，未读凭据)（认证头：Token <不回显>）
exit=0
```

（上面 `exit=-1` 是 PowerShell 对 `| Select-Object -First N` 截断管线报的假信号；
不经管线单独跑是 `exit=0`。桩测试 `:263` 那条在**桩已关闭**的前提下断言 exit=0 且 `stub.recorded == []`，
是这一条更强的机器证据。）

反向对照——**真有可发的请求时 dry-run 长什么样**（`--case-map` 指向一份只登记了
LC-01→101、LC-06→{106, version 2} 的临时表；`ZENTAO_URL` 故意设成打不通的
`http://127.0.0.1:1/` 且**不设** `ZENTAO_TOKEN`）：

```
> python scripts/report_to_zentao.py --dry-run --junit-xml <...>\login.xml --case-map <...>\map.json
待发送 2 条请求 → (dry-run，未读凭据)（认证头：Token <不回显>）
  POST /api.php/v1/testcases/101/results?testtask=0
       case_id=LC-01 nodeid=testcases/login/test_login_contract.py::test_lc01_login_success result=pass
       body={"steps": [{"result": "pass", "real": "fullstack-test-framework 自动上报：passed；nodeid=testcases/login/test_login_contract.py::test_lc01_login_success"}]}
  POST /api.php/v1/testcases/106/results?testtask=0&version=2
       case_id=LC-06 nodeid=...test_lc06_to_lc13_missing_fields_400[payload0-LC-06 缺全部字段] result=pass
exit=0        # 无凭据、URL 打不通，dry-run 照样跑完 —— 它根本没联网
```

同一命令改 `--send`：`exit=3`、stderr 首行 `缺少环境变量：ZENTAO_TOKEN`，且**连产物都不写**
（`p2.json` 不存在）——凭据缺失是**前置检查**，不是发到一半才失败。

`2 条待发` 与 `28 条 skipped` 同时出现（30 个登录编号里只登记了 2 个）——「编号↔结局」
join 与「编号↔禅道 ID」登记是**两条独立的闸门**：前者由 junit + case-index 决定，
后者由 `docs/zentao-case-map.json` 决定，缺后者只跳过、不猜也不静默。

### 4.5 自检（裸命令，仓库根）

```
> & $PY -m ruff check .
All checks passed!
exit=0

> & $PY -m ruff format --check .
97 files already formatted
exit=0

> & $PY -m mypy
Success: no issues found in 77 source files
exit=0
```

---

## 5. 整合清单（改了哪些文件，供合进原仓库）

### 5.1 新增（3 个文件，全部直接入仓）

| 文件 | 行数 | 说明 |
|---|---|---|
| `scripts/case_index.py` | 532 | 零新增依赖（stdlib：argparse/ast/json/re/subprocess/sys/dataclasses/pathlib/datetime） |
| `scripts/report_to_zentao.py` | 453 | 零新增依赖（stdlib：urllib/http/json） |
| `scripts/test_report_to_zentao_stub.py` | 431 | 零新增依赖（stdlib：http.server/threading/subprocess），`pytest` 已在依赖里 |

### 5.2 新增（`docs/zentao-case-map.json`，**必须入仓**）

`reports/` 被 `.gitignore:10` 忽略，而禅道用例 ID 是人工资产不是产物，所以这张表落在
`docs/zentao-case-map.json`（`git check-ignore` 未命中）。当前 `cases` 为空对象——**这是正确状态**，
不是漏填。

### 5.3 外壳接线（第二份任务书授权后**已落地**，见 §8）

第一轮交付时这五条被判为「不在子任务边界内」，调度者随后把其中四条正式授权，
已在同一工作副本上追加三条提交（`13c6ecb` / `0751b09` / `a716c22`）。逐条结果：

| 文件 | 落地内容 | 理由 |
|---|---|---|
| `README.md` | 新增一节 `### 用例编号与禅道上报的起点`（`:323`） | 紧贴「用例设计与编号」主题，不塞进「当前实测结果」 |
| `CHANGELOG.md` | 新增一节 `### 新增 —— 用例编号到禅道用例 ID 的上报链路（波 3 · P2-1）`（`:143`） | 与波 1/波 2 的写法一致；按授权要求插在 flaky 治理（波 2）之后、依赖升级修复之前 |
| `.github/workflows/test.yml` | `test` job 内两个 step：桩测试 + `case_index.py --verify` | `pytest.ini` 的 `testpaths = testcases` 不会自动收集 `scripts/` 下的测试，不加就没人跑。**不改变全仓收集数**（181 不变） |
| `.env.example` | 追加 `ZENTAO_URL` / `ZENTAO_TOKEN` / `ZENTAO_PRODUCT` 三个**占位符** | 与仓库既有的三条规则一致（只从 .env 或 CI secret 注入、值是占位符、已存在的环境变量优先） |

**接线时必须守住的四条**（已核对，可逐条 grep 复核）：未新开 job；未改任何
`timeout-minutes`（`test` job 仍是 `45`）；未新增或改动任何 `continue-on-error`；
新增两步都不设 `PYTHONPATH`（设了收集数会从 181 涨到 369）。

**未做、也不该由本轮做的一条**：`docs/质量门禁策略.md` 与 `scripts/check_gate_policy.py`
（P2-4 的交付物）在本工作副本里**不存在**，本轮没有创建也没有修改它们。因此上面两个新
step **尚未登记进策略文档**——按 P2-4 侧的三方一致性校验（策略文档 × test.yml ×
pytest.ini）口径，这是需要转告的一条，不能默认已被覆盖。

### 5.4 **不需要**改的东西

- `pytest.ini` 的 `addopts`：本轮不动任何默认行为，也没新增 marker。
- 根 `conftest.py`：`reports/flaky-governance.json` 的形状已经够上报脚本用，不必扩展。
- `testcases/**`：**一个字都没改**。本轮所有编号都是从已有写法里读出来的——
  没有重命名函数、没有改 parametrize 的 id、没有加 docstring。既有报告的可比性完好。
- `requirements.txt` / `requirements.lock`：零新增依赖。

---

## 6. 未验证项清单（每一条都需要真实禅道实例 / 别的手续）

**A. 必须真实禅道实例才能确认**

1. `POST /api.php/v1/testcases/{id}/results` 在**开源版**上是否真能落库。已归档的论坛帖报过「不起作用」。
2. 该端点是否必须 `testtask` 非 0 才更新测试单结果。当前脚本默认发 `testtask=0`（取值来自 `docs/zentao-case-map.json` 的 `_testtask`），
   禅道是否接受「不关联测试单」的语义**未验证**。
3. `version` 省略时是否真的取最新版本；发 `version` 会不会因为版本号不存在而报错。
4. 成功响应体形状（文档只说 `message` 成功时返回空数组），以及 `id` 究竟是**用例 ID** 还是**用例-测试单关联 ID**。
5. `Token` header 的大小写是否被服务端网关严格校验（脚本按 v1 文档发大写 `Token`）。
6. `GET /api.php/v1/products/{id}/testcases` 的响应字段能否反查到「禅道用例 ID ↔ LC 编号」——
   即 §5.2 那张表能否半自动化生成。目前是纯手工。
7. 禅道侧承载「LC-01」编号的约定（写进 `title` 前缀？还是不加）——本脚本不对禅道侧写法做任何约束。
8. v1 RESTful API 的最低版本门槛（手册提到 16.5+，但本次抓到的页面正文未能复现该结论）。

**B. 推断（已在 §2 标注，不得当事实用）**

- Z11：v2 的「执行用例结果」端点存在与否。**本脚本不依赖它**。
- 取 token 端点：v1 用 `/api.php/v1/tokens`（文档 1397，确认），v2 用 `/api.php/v2/users/login`（文档 2142，确认）——两者**不是同一个**。
  若真实实例只支持 v2 认证，需改 `report_to_zentao.py` 的取 token 部分（**当前脚本根本不取 token，只要求人给 `ZENTAO_TOKEN`**，
  所以这条对本轮交付无阻塞）。

**C. 本轮已实测但与真实禅道无关的注意点**

- `pytest testcases --collect-only`（不带 `PYTHONPATH`）退出码是 **1**，因为 `testcases/contract/test_schemathesis_properties.py` 收集报错
  （`schemathesis` 在 `work/pylibs` 而非 venv 里）。`case_index.py` 用 `--continue-on-collection-errors` 取到 181 个 nodeid 后照常工作，
  并把 `collection.exit_code = 1` 如实写进产物。
- 带 `PYTHONPATH=work/pylibs` 时全仓收集数从 **181 暴涨到 369**（pylibs 里 schemathesis/hypothesis/locust 自带的 conftest 测试被收集）。
  **跑全仓命令不要设 `PYTHONPATH`。**

---

## 7. 提交记录（在 `0e61618` 之上 7 条，一事一提交）

### 第一轮：四项交付物（4 条）

```
> git log --oneline 0e61618..ce8bdec
ce8bdec docs: 补波 3 禅道对接的交接文档
cefff5c feat(scripts): 新增禅道上报脚本与本地桩服务测试
6c888d5 chore(docs): 新增用例编号到禅道用例 ID 的映射表
0a327e7 feat(scripts): 新增用例编号到 nodeid 的索引脚本
```

### 第二轮：外壳接线（3 条，第二份任务书授权）

```
> git log --oneline ce8bdec..HEAD
a716c22 chore: 登记禅道上报凭据的占位符
0751b09 ci: 把禅道桩测试与用例编号索引自检接入 CI
13c6ecb docs: 补用例编号与禅道上报链路的说明
```

```
> git diff --stat 0e61618..HEAD
 .env.example                          |  15 +
 .github/workflows/test.yml            |  24 ++
 CHANGELOG.md                          |  38 +++
 README.md                             |  45 +++
 docs/wave3-zentao-handoff.md          | 615 ++++++++++++++++++++++++++++++++++
 docs/zentao-case-map.json             |  11 +
 scripts/case_index.py                 | 532 +++++++++++++++++++++++++++++
 scripts/report_to_zentao.py           | 453 +++++++++++++++++++++++++
 scripts/test_report_to_zentao_stub.py | 431 +++++++++++++++++++++++
 9 files changed, 2164 insertions(+)
```

纯新增，零删除——`testcases/**` 一个字都没改（§5.4）。

产物（`reports/case-index.json`、`reports/zentao-payload.json`）**不在**上面这九行里——
它们被 `.gitignore:10` 的 `reports/` 忽略，是运行产物不是交付物。

`origin/main` 停在 `c2098f5`（波 1 时的旧远端），`git rev-list --left-right --count origin/main...HEAD`
= `0  37` —— **本轮没有 push，也不该 push**：交付是「整合清单 + 本地提交」，由调度者决定怎么合。

---

## 8. 第二轮（外壳接线）的验收证据

### 8.1 数字可复现

`python scripts/case_index.py`（无 `PYTHONPATH`）→ **exit 0**，关键行：

```
测试函数: 148
收集到 nodeid: 181
有编号的 nodeid: 27
无编号的 nodeid: 154   <-- 覆盖缺口，不掩饰
编号条目: 30（已解析 30 / 未解析 0）
去重编号: 30    带编号的函数: 17
无编号 nodeid 按套件: mes: 55, api: 47, contract: 36, ui: 14, login: 2
一条 nodeid 承载多个编号（缺陷定位精度到此为止）:
  testcases/login/test_login_contract.py::test_lc22_to_lc25_decision_table  ->  LC-22、LC-23、LC-24、LC-25
```

同一命令加 `PYTHONPATH=work/pylibs`（即 README 表里 353 那组的来源）→ **exit 0**：
`收集到 nodeid: 369` / `无编号的 nodeid: 342` / `无编号 nodeid 按套件: contract: 224, mes: 55, api: 47, ui: 14, login: 2`
（224+55+47+14+2 = 342 ✓）。README 表的 353 那组来自已合入波 3 全部改动的整合区（380 条收集）。

README 只钉**与依赖和文件集无关**的那组：`LC` 30 个编号 / 17 个函数 / 27 个带编号 nodeid / 未解析 0。

### 8.2 `testcases/**` 零改动

```
> git diff --name-only 0e61618..HEAD -- testcases     → 0 行（输出为空）
> git diff --stat 0e61618..HEAD -- testcases          → 输出为空
> git status --porcelain -- testcases                 → 0 行
```

### 8.3 `--verify` 门禁的反向验证（证明它真的会红）

`--testcases-dir` 指向工作副本之外的路径会撞上 `scan()` 里
`path.relative_to(REPO_ROOT)`（`scripts/case_index.py:253`）而抛 `ValueError`——这是**用法限制
而非门禁失效**，为把反向验证做成受控实验，改用整树复制：

1. `robocopy <工作副本> <_tmp\gate> /MIR`（排除 `.git`/`.venv` 等）；在副本里给
   `testcases/login/test_login_contract.py` 顶部插入一行必然 `ImportError` 的 import
   ——模拟「用例被改名/挪走、编号还留在文档里」，该模块收集不到任何 nodeid。
2. 在副本里跑 `python scripts/case_index.py --verify` → **exit 3**，stderr：
   ```
   未解析编号 30 条
   ```
   产物里 30 条 `unresolved` 逐条列出（`LC-01` … `LC-30`）。
3. 在**完好的**工作副本里跑同一命令 → **exit 0**。
4. 还原证据：副本目录已删除；主仓 `git status --porcelain` 只剩 4 个已提交文件的
   ` M`（提交前状态），`git diff --stat -- scripts/case_index.py` 输出为空
   ——`case_index.py` 与 HEAD 逐字节一致，没有留下任何临时退化改动。

### 8.4 桩测试真跑

```
> python -m pytest scripts/test_report_to_zentao_stub.py -q
scripts\test_report_to_zentao_stub.py .........                          [100%]
9 passed in 8.20s      → exit 0
```

### 8.5 三条裸自检（仓库根，不带参数）

```
> python -m ruff check .          → All checks passed!                    exit=0
> python -m ruff format --check . → 97 files already formatted            exit=0
> python -m mypy                  → Success: no issues found in 77 source files   exit=0
```
