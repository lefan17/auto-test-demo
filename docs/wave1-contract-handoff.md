# 波 1 · P0-2 契约驱动改造 — 交接文档

面向整合者。本文件只写证据：改了哪些文件、命令输出原文、未解决项。

- 工作仓库：`C:\Users\lefan\DSH_project\接口自动化_3\work\w1-contract`
- 基线 commit：`691cb04 chore: mirror baseline from auto-test-demo@b8524c9`
- 解释器：`C:\Users\lefan\DSH_project\DH_1\fullstack-test-framework\.venv\Scripts\python.exe`（Python 3.12.10）
- 每次跑 pytest/ruff/mypy 前设：`$env:PYTHONPATH='C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'`
  （**2026-10 订正**：schemathesis 升到 4.31.1 后已装进 `.venv`，本行不再需要——见本文件 §5「三条必须知道的使用约束」的订正）

---

## 1. 改动清单（改了什么、为什么）

### 新增

| 文件 | 行数 | 为什么 |
| --- | --- | --- |
| `scripts/export_openapi.py` | 134 | 契约必须与代码同源。原来 `testcases/login/contract.py` 是人肉维护的 dict，实现一改就脱节且没人知道。这个脚本从 `app.openapi()` 直接导出，产物入库，`--check` 模式做逐字节漂移检测 —— 这是「契约 diff 门禁」的落点。 |
| `docs/openapi.json` | 3230 行 / 80867 字节 | `mock_server/app.py` 的自动导出产物（OpenAPI 3.1.0，23 paths，29 operations）。入库才能被 CI 逐字节比对。 |
| `docs/openapi-login_app.json` | 62 行 / 2129 字节 | 手写契约描述的是 `login_app`，不是 `mock_server`（见 §3），是**两个服务**。只导 mock_server 的话，手写契约就没有可对照的自动产物，对照结论无从谈起。 |
| `testcases/contract/__init__.py` | 1 | 包标记。 |
| `testcases/contract/spec_contract.py` | 276 | 本模块的核心设计决定所在：定义**纯净视图**（入库的 `docs/openapi.json`，任何测试不得修改，是漂移门禁基准）与**测试用补全视图**（内存里补 FastAPI 结构上无法表达、但服务端确实会返回的东西，只给 schemathesis 用，不写盘）。详见 §4。 |
| `testcases/contract/conftest.py` | 115 | session 级起 Mock 服务（复用 `testcases/mes/mock_server_ctl.py` 的 `MockServer` 类，不写第二份启动逻辑）+ 各角色令牌 + spec fixture + 规模旋钮。 |
| `testcases/contract/test_contract_export.py` | 288 | ① 门禁测试：验证 `--check` 真能挡住漂移（篡改文件、删文件两种情况）；② 缺陷钉住：用 `strict=True` 的 xfail 把已确认为真实缺陷的期待断言下来。 |
| `testcases/contract/test_handwritten_parity.py` | 158 | 手写契约 vs 自动导出的逐条对照。 |
| `testcases/contract/test_schemathesis_properties.py` | 361 | schemathesis 属性驱动测试：正向生成必须符合 spec、负向生成必须被拒、认证/鉴权矩阵逐格断言。 |

### 修改

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `pytest.ini` | markers 新增一行 `contract: ...` | `--strict-markers` 下不注册会直接报错。 |
| `requirements.txt` | 追加「契约驱动测试」段，pin `schemathesis==3.39.16` | 只 pin 直接依赖，传递依赖交给 pip 解析（理由写在文件注释里）。 |
| `pyproject.toml` | 原「债 1」拆成 `apis.base`（`assignment` + `attr-defined`）与 `apis.mes_api`（`assignment`），新增债 1b 注释 | mypy 原来是**红的**（`apis/base.py:111-112` 两个 `attr-defined`），已在 691cb04 的独立 worktree 上复现同样的两行报错，确认是既有基线债而非本次引入。 |

`json` 产物的字节指纹（`Get-FileHash`）：

```
docs/openapi.json            80867 字节
docs/openapi-login_app.json   2129 字节
```

### 提交记录（在 `691cb04` 之上 5 条，全部通过 `scripts/check_commit_msg.py`）

查询方式：`git log --oneline 691cb04..HEAD`。5 条依次是：
`feat(contract)` 导出脚本 + 两份 spec 产物 → `test(contract)` 契约测试套件 →
`build(deps)` schemathesis 依赖 → `chore(mypy)` 基线债登记 + contract marker →
`docs(contract)` 本文件。

提交后复核：`git status --porcelain` 为空；`--check` 仍报 `[一致]`；
`git diff --stat HEAD -- docs/openapi*.json` 为空（入库字节 == 工作区字节）；
`pytest -m contract -q` → `203 passed, 12 xfailed in 19.25s`。

`--check` 的实测输出见 §7 第 ⑧ 项。

---

## 2. `openapi.json` 的路径、导出方式、可达性

- 路径：`docs/openapi.json`（放在 `docs/` 而不是仓库根，因为 `docs/` 已经是契约类文档（`login-contract.md`、`login-test-cases.md`）的归属地；根目录已有较多文件，且 spec 是会随代码变的产物，与 `README`/`CHANGELOG` 这类手工文档混在一起反而不好找）。
- 导出方式：一条命令 `python scripts/export_openapi.py`，导两个服务；`--app mock_server|login_app|all` 可只导一个。
- 可复现性：脚本每次用 `tempfile.TemporaryDirectory()` 造临时 SQLite 路径再 `create_app(db_path=...)`，**不会在仓库里留下 `dsh_mes.db`**。序列化固定为 `json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"`，并显式 `newline="\n"` 写盘（对齐 `.gitattributes` 里 `*.json text eol=lf`）。
- 踩过的坑（已修，记录给整合者）：脚本最初在 Windows GBK 控制台下写中文报 `UnicodeEncodeError`，现已在 `main()` 开头对 `stdout`/`stderr` 强制 `reconfigure(encoding="utf-8")`。

---

## 3. 手写契约 vs 自动导出：逐条对照结论

### 3.1 最重要的结论：两者描述的是**两个不同的服务**

| | 手写契约 `testcases/login/contract.py` | 自动导出 `docs/openapi.json` |
| --- | --- | --- |
| 被测体 | `login_app/app.py` | `mock_server/app.py` |
| 路径前缀 | `/api` | `/api/v1` |
| title | `AgentTest Demo — 登录服务` | `DSH MES Mock API` |
| 路径数 | 3 | 23 |
| operation 数 | 3 | 29 |

手写契约里的 `/api/health`、`/api/login`、`/api/flaky` 三条路径，在 `docs/openapi.json` 里**一条都不存在**（该 spec 的 23 条 path 全部以 `/api/v1` 开头）。

- 依据：`testcases/login/contract.py` 的 `title` 与 `login_app/app.py` 的 `APP_TITLE` 逐字相同；三条路径能在 `login_app/app.py` 里逐条找到（`/api/health`、`/api/login`、`/api/flaky`），而 `mock_server/routers.py` 里没有任何一条。
- 这不是「谁对谁错」的问题——是任务书里「导出的 spec 要与手写契约对得上」这条要求隐含了一个错误前提。所以脚本**同时导出两份 spec**，对照才有意义。这一点没有偷偷改任何一方来凑。

### 3.2 三条路径的逐条对照（`test_handwritten_parity.py` 实测）

| 手写契约声明 | 自动导出（`docs/openapi-login_app.json`） | 结论 |
| --- | --- | --- |
| `/api/health` GET | 存在，responses 只有 `200` | 路径一致；状态码手写更准（见下） |
| `/api/login` POST | 存在，responses **只有 `200`** | 路径一致；**状态码不一致** |
| `/api/flaky` GET | 存在，responses 只有 `200`（description 里文字提到约 10% 概率 503） | 路径一致；**503 未声明** |

### 3.3 三个状态码语义（200/400/401）的对照

手写契约声明 `{200: 登录成功, 400: 缺用户名密码或非法 JSON, 401: 密码错误}`。实测：

| 状态码 | 手写契约 | 自动导出 login spec | 实现真实行为 | 判定 |
| --- | --- | --- | --- | --- |
| 200 | 声明 | 声明 `200` | 实测返回 200（带 token） | **一致** |
| 400 | 声明 | **未声明** | 实测返回 400 | 手写更准 → 钉为 DEF-04 |
| 401 | 声明 | **未声明** | 实测返回 401 | 手写更准 → 钉为 DEF-04 |

### 3.4 手写契约自己的遗漏（一并记录，不偷改）

- 手写契约只列了 200/400/401，但 `login_app/app.py` 实现里还有 **404**（用户不存在分支）与 **503**（flaky 端点）。
- 手写契约 `undeclared_status_codes()` 返回 `{405, 422, 500}` —— 这是作者当时认为「不该出现」的码，不是「已声明」的码，语义与 paths 不同，对照时不能混用。

---

## 4. 为什么需要「测试用补全视图」（关键设计决定）

**问题**：`docs/openapi.json` 里 29 个 operation 的 responses 只有 `200`/`201`/`422`。直接拿它跑 schemathesis，服务端**正确**返回的 400/401/403/404/409 会因为「spec 未声明」被判成失败 —— 用假阳性把真阳性淹掉。

**被否掉的两个方案**：

1. 把 400/401/403/404/409 直接写进 `docs/openapi.json`。否掉的理由：产物是从 `app.openapi()` 逐字节导出的，手工往里加内容会让漂移门禁退化成「文件和自己一致」，既挡不住实现漂移，也把「spec 欠声明」这个真问题掩盖了。
2. 用 `excluded_checks` 把 `status_code_conformance` 整个关掉。否掉的理由：关掉之后「服务返回了 spec 没声明的 500」也一并放行了，门禁失效。

**采用的方案**：`spec_contract.py:build_test_view()` 在内存里构造一份补全视图，只给 schemathesis 用，不写盘。补的东西逐条有出处：

| 补什么 | 依据 |
| --- | --- |
| `components.securitySchemes.bearerAuth` + 受保护 operation 的 `security: [{bearerAuth: []}]` | 鉴权实现是 `authorization: str | None = Header(default=None)`（`mock_server/deps.py:34`），FastAPI 只能导出成可选普通 header 参数 |
| 删掉原 `authorization` header 参数 | 否则 hypothesis 会给它生成随机值，覆盖真实令牌（实测生成过 `'$\x1bä×<'`） |
| 401（受保护端点）、403（有权限动作）、400（有 query 或 body）、404（有 path 参数）、409（body 含 `version`） | `mock_server/app.py` 的全局 `exception_handler` 与 `mock_server/errors.py` 的错误码表；每条都在测试里用真实请求复现过（`test_live_error_status_codes_are_reachable`） |
| 把自动生成的 422 schema（`HTTPValidationError = {detail: 数组}`）换成服务端实际产出的错误信封 | 服务端 422 实际是 `{code, message, detail:{errors:[...]}}` |

**纯净 spec 与补全视图的差异本身也被测试钉住了**：`test_spec_declares_bearer_auth_scheme`、`test_reachable_error_status_is_declared_in_spec` 断言纯净 spec **应该**声明这些而现在没有 → xfail(strict=True)，一旦有人补上就 XPASS 报红。

---

## 5. schemathesis 实测配置与耗时

| 配置 | 环境变量 | 每个 operation 的 examples | 属性套件耗时 |
| --- | --- | --- | --- |
| 默认（CI 用） | 无 | 8（负向 4） | **11.27s**（`178 passed, 1 xfailed`） |
| 放大 | `CONTRACT_FULL=1` | 50（负向 25） | **44.89s**（`178 passed, 1 xfailed`） |
| 指定值 | `CONTRACT_MAX_EXAMPLES=50` | 50 | 同上 |
| 负向单独指定 | `CONTRACT_NEGATIVE_MAX_EXAMPLES=N` | — | 默认 `max(2, max_examples()//2)` |

补充：schemathesis 3.39 **没有**内建的 max-examples 环境变量（`schemathesis/constants.py` 里不存在），所以规模旋钮是自己实现的。放大 6 倍 examples 只把耗时从 11.27s 拉到 44.89s（线性），没有超线性爆炸。

（**2026-10 复核**：升到 4.31.1 后结论不变——`--max-examples` 仍只是 CLI 选项，没挂 envvar，所以自实现的 `CONTRACT_MAX_EXAMPLES` / `CONTRACT_FULL` 旋钮继续有效。）

三条必须知道的使用约束：

1. ~~**OpenAPI 3.1 是实验特性**。FastAPI 0.115.6 默认导出 3.1.0，schemathesis 3.39 直接拒绝（`SchemaError: The provided schema uses Open API 3.1.0, which is currently not fully supported.`）。代码里在 import 期调 `schemathesis.experimental.OPEN_API_3_1.enable()`。**升级 schemathesis 前必须先确认这个开关还在。**~~
   **⚠️ 2026-10 订正（schemathesis 3.39.16 → 4.31.1）**：上面这条已经失效。4.x **原生支持** OpenAPI 3.1，
   `schemathesis.experimental` 整个模块被删除（4.31.1 wheel 里没有该目录），契约测试里原先那句 import 期的
   `schemathesis.experimental.OPEN_API_3_1.enable()` 已随之删掉，**不再需要任何开关**。
2. 认证走 `schemathesis.auths.AuthProvider`（`BearerAuth` 类：`get()` 返回令牌、`set()` 写进 `case.headers`），用 `schema.auth.apply(BearerAuth)` 注册。不用「手工塞 header」是因为补全视图声明了 `bearerAuth` 后 schemathesis 会自己生成随机 `Authorization`，塞进去的会被覆盖。
3. `schema.get_all_operations()` 返回的是 `Ok`/`Failure` 包装对象，必须 `.ok()` 才拿到 `APIOperation`。

---

## 6. schemathesis 发现的真实缺陷清单

判定标准：**期望来自服务端自己声明的契约（spec 或代码注释），实际来自真实 HTTP 响应，且复现用的是标准请求**。凡是我自己用法有问题导致的失败，一律不计入（本轮有 2 个：一个是 `Session.request(content=...)` 参数名写错，一个是负向生成把 `page_size` 生成成 0 时的 `negative_data_rejection` 语义与分页语义不对齐，均已按用法问题修正）。

### DEF-01：422 响应体违反 spec 自己声明的 schema

- **method + path**：`POST /api/v1/materials`（同类还有 `POST /api/v1/auth/login`、`POST /api/v1/inventory/issue`、`POST /api/v1/inventory/reverse`、`POST /api/v1/work-orders`、`GET /api/v1/materials`、`GET /api/v1/inventory/stock`、`GET /api/v1/inventory/ledger`，共 8 个 operation 实测命中）
- **请求**：`POST /api/v1/materials`，body `{"material_code":"MAT-RECON-02"}`（缺必填字段）
- **期望**：spec 声明 422 的 schema 是 `HTTPValidationError`，即 `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`（`detail` 是**数组**）
- **实际**：`422 {"code":"REQUEST_VALIDATION_ERROR","message":"请求体校验失败","detail":{"errors":[{"field":"name","type":"missing","reason":"Field required"}]}}`（`detail` 是**对象**）
- **schemathesis 报错原文**：`None is not of type 'array'`，`Schema at /properties/detail: {"items": {"$ref": "#/components/schemas/ValidationError"}, "title": "Detail", "type": "array"}`，`Value: null`
- **为什么是服务的问题**：这个 422 schema 不是外部写的，是 FastAPI 依据**同一个 app** 的 `RequestValidationError` 处理链自动生成的；而 `mock_server/app.py` 自己注册的 `validation_handler` 返回的信封形状与自动生成的声明不一致。**声明与实现出自同一个代码库却互相矛盾**，与调用方用什么工具无关。
- **钉住位置**：`test_contract_export.py::test_live_422_body_conforms_to_declared_schema`（xfail strict=True，用 `jsonschema.validate` 对照 spec 声明的 schema 校验真实响应体）

### DEF-02a：鉴权在 spec 里没有语义

- **method + path**：全部 26 个受保护 operation
- **期望**：`components.securitySchemes` 声明 `bearerAuth`，受保护 operation 带 `security`
- **实际**：`"securitySchemes" in spec["components"]` → `False`；每个 operation 的 `security` 为 `None`；`authorization` 只是一个**可选**普通 header 参数
- **为什么是服务的问题**：鉴权实现写成 `authorization: str | None = Header(default=None)`（`mock_server/deps.py:34`）后手工解析，FastAPI 的结构化安全声明（`Depends(HTTPBearer())` / `Security()`）没被使用，于是「这个端点需要令牌」这条信息在机器可读契约里**完全不存在**。任何消费 spec 的工具（schemathesis、代码生成器、文档站）都只能把受保护端点当成公开端点。
- **后果（实测）**：不加任何补偿直接跑，schemathesis 会把 401 判成 `Undocumented HTTP status code` 失败；补全视图必须手工把 `security` 补回来才能测认证。
- **钉住位置**：`test_contract_export.py::test_spec_declares_bearer_auth_scheme`（xfail strict=True）

### DEF-02b：400/401/403/404/409 一条都没声明

- **method + path**（`UNDECLARED_BUT_REACHABLE` 六条代表）：
  - `GET /api/v1/materials` → 401（无令牌）/ 400（`?page=0`）
  - `POST /api/v1/materials` → 403（QA 角色）
  - `GET /api/v1/materials/{material_code}` → 404（不存在）
  - `PUT /api/v1/materials/{material_code}` → 409（version 不匹配）
  - `POST /api/v1/auth/login` → 401（口令错）
- **期望**：这些状态码在 spec 的 `responses` 里被声明
- **实际**：29 个 operation 的声明状态码**只有** `200`/`201` + `422`（`/healthz`、`/readyz` 只有 `200`）
- **为什么是服务的问题**：这些码由 `mock_server/app.py` 的全局 `exception_handler`（`BizError` → `exc.status`）与 `mock_server/errors.py` 的错误码表产出，是**设计好的业务语义**（409 乐观锁冲突、403 权限矩阵、400 分页/排序边界），不是意外；但 FastAPI 的生成器看不到 `BizError` 的子类，于是整类语义在契约里消失。
- **原始证据**：第一轮 CLI 扫描 `--checks all --hypothesis-max-examples 8` → `3 passed, 26 failed in 10.56s`，失败里 `Undocumented HTTP status code` ×22（Received: 404 ×17、400 ×5）、`Response violates schema` ×8。
- **钉住位置**：`test_contract_export.py::test_reachable_error_status_is_declared_in_spec`（6 条 parametrize，xfail strict=True）+ `test_live_error_status_codes_are_reachable`（正向复现 6 个码，必须一直绿 —— 它是 DEF-02 的证据本身）

### DEF-03：文档宣称支持的表单登录实际不可用

- **method + path**：`POST /api/v1/auth/login`
- **期望**：`mock_server/routers.py:71-75` 的 description 原文宣称「同时接受 `application/json` 与 `application/x-www-form-urlencoded`」→ 正确凭据的表单提交应得 200
- **实际**：
  - 表单 `username=admin&password=admin123`（正确凭据）→ **422** `{"code":"REQUEST_VALIDATION_ERROR","detail":{"errors":[{"field":"","type":"dict_type","reason":"Input should be a valid dictionary"}]}}`
  - 空表单 → **500** `{"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"AssertionError"}}`
- **机制（已定位到行）**：`routers.py:83` 的签名是 `body: dict = Body(default_factory=dict)`，FastAPI 在**进入函数体之前**就做请求体校验，非 JSON 内容体一律先在 `body` 上炸成 422 `dict_type`，根本走不到 `routers.py:86` 的 `if "application/x-www-form-urlencoded" in content_type` 分支；只有 body 为空时才进入函数体走到 `request.form()`，而此时环境里没装 `python-multipart`（实测 `importlib.metadata.version('python-multipart')` → `PackageNotFoundError`）→ `AssertionError: The python-multipart library must be installed to use form parsing.` → 全局兜底 handler 转成 500。服务端日志已确认该 AssertionError 出现 2 次，栈顶为 `starlette/requests.py:255 in _get_form`。
- **为什么是服务的问题**：用的是标准 `urllib` 发标准 urlencoded 表单，不是畸形请求；服务自己的文档白纸黑字承诺了这个能力。
- **钉住位置**：`test_contract_export.py::test_declared_form_login_returns_token`（xfail strict=True）+ 三个现状取证用例 `test_login_description_claims_form_support`、`test_declared_form_login_is_actually_rejected`、`test_empty_form_login_returns_500`

### DEF-04：手写契约的 400/401 语义在自动导出里丢失

- **method + path**：`POST /api/login`（`login_app`）
- **期望**：`docs/openapi-login_app.json` 声明 400 与 401（手写契约与实现都有）
- **实际**：只声明 `200`；而 `/api/login` 的 description 里用文字写了「返回 200 / 400 / 401 三种结果」
- **为什么是服务的问题**：`login_app/app.py:87` 的 `login()` 返回 `JSONResponse` 并在**函数体内部**直接给 `status_code`，FastAPI 的生成器只能从返回注解推导，看不到函数体内的分支 → 契约的机器可读部分与文字部分、与实际行为三者不一致。
- **附带结论**：这是本任务中**手写契约比自动导出更准确**的反例（另一处是 503 未声明）。自动导出不是无条件更好的，所以手写契约保留不动。
- **钉住位置**：`test_handwritten_parity.py::test_auto_export_documents_declared_login_status[400|401]`（xfail strict=True）

### DEF-05：非有限十进制字面量打穿服务（500 而非 422）

- **method + path**：`POST /api/v1/inventory/issue`（同类还有 `POST /api/v1/materials`、`POST /api/v1/work-orders`、`POST /api/v1/work-orders/{order_no}/report` 等所有吃数量字段的端点）
- **请求**：body 里数量字段传非有限十进制字面量，如 `{"material_code":"MAT-CU-001","warehouse":"WH-01","qty":"INFINITY"}`
- **期望**：输入格式问题 → 422（与同文件其它非法输入的处理一致）
- **实际**：**500** `{"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"InvalidOperation"}}`
- **机制（已定位到行）**：`mock_server/quantity.py:49` 的 `Decimal(str(value))` 对 `'INFINITY'`/`'NaN'`/`'1e999999'` **能构造成功**（不抛 `InvalidOperation`），真正的异常发生在 `quantity.py:53` 的 `dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)`；而 `try/except (InvalidOperation, ValueError)` 只包住了第 49 行的构造，`quantize` 在 try 之外 → 异常穿透到 `mock_server/app.py` 的兜底 `Exception` handler → 500。
- **为什么是服务的问题**：这是输入校验的**漏网分支**，不是「服务确实想返回 500」。同一函数对 `'abc'` 能正确回 422，说明作者意图是「非法数字 → 422」，只是非有限值是 `Decimal` 能接受、`quantize` 才炸的一类特例。
- **钉住位置（已随修复更新，见 §10.1）**：本条**已在波 1 收口时修好，不再是 xfail**。现在由三条**正常**用例固化：`test_schemathesis_properties.py::test_non_finite_decimal_literal_is_rejected_as_422`（6 组参数化，断言 422 + `code=VALIDATION_ERROR` + 出错字段名）、`test_non_finite_decimal_parser_contract`（不经 HTTP，直接钉住 `parse_qty` / `parse_qty_allow_zero` / `parse_cent` 三个函数都不漏）、以及负向用例里「任何 5xx 一律失败」的硬断言。
- **注意**：这个缺陷是 schemathesis 的**负向生成**扫出来的（hypothesis 生成了 `'INFINITY'` 这类字面量），属于属性测试真正的收获 —— 手写用例基本不可能覆盖到。
- **同时删掉了修复前的补偿机制**：修好之前，属性套件里有一套定向分类逻辑（`spec_contract.py` 的 `NON_FINITE_LITERAL` 正则 + `contains_non_finite_literal()` + `case_contains_non_finite_literal()`，以及负向用例里的 `known_defect` 记账），作用是「输入照发、响应照看，但踩中 DEF-05 的 500 不算新信号」，避免每次跑都被同一颗地雷刷屏。**修复后这整套补偿逻辑已从代码里删除**（`spec_contract.py` 不再 `import re`），正向与负向两条路径都不再有任何「已知缺陷」豁免 —— 现在任何 5xx 都会让用例失败。这正是任务书要求的「不许为了让套件变绿而在正向路径加宽泛的已知缺陷分类」。

### DEF-06：超大 `page` 打穿 SQLite INTEGER（500 而非 400）

- **method + path**：`GET /api/v1/inventory/ledger?page=461168601842738792`（同类还有 `GET /api/v1/materials`、`GET /api/v1/work-orders`、`GET /api/v1/inventory/stock` 等所有走 `get_pagination` 的列表端点）
- **期望**：页码超出可寻址范围 → 400，与既有 `page < 1 → 400 PAGINATION_OUT_OF_RANGE` 同类同码
- **实际**：**500** `{"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"OverflowError"}}`
- **机制（已定位到行）**：`mock_server/deps.py` 的 `page: int = Query(default=1)` **只有下界没有上界**，`Pagination.offset = (page - 1) * page_size` 一旦超过 SQLite 的有符号 64 位 INTEGER（`2**63 - 1`），sqlite3 抛 `OverflowError: Python int too large to convert to SQLite INTEGER`；该异常不属于 `BizError`，逃到 `mock_server/app.py` 的兜底 `Exception` handler → 500。
- **为什么是服务的问题**：这纯粹是**输入范围**问题，服务端本可以在拼 offset 之前就算出来并拒掉，没有任何理由让数据库驱动的溢出异常变成 500。同一依赖函数对 `page=0` 已经会正确回 400，说明作者意图是「页码不合法 → 400」，只是漏了上界。
- **钉住位置（收口时已修，见 §10.2）**：`test_schemathesis_properties.py::test_out_of_range_pagination_is_rejected_not_500`（正常用例，4 组参数化）、`test_pagination_boundary_is_where_sqlite_integer_ends`（不经 HTTP 直接钉住边界**位置**，防止界被改小/改大）、以及 `test_contract_export.py` 里既有的 `page=0 → 400` 断言（防止修法把下界行为改回归）。
- **注意**：这条是**把样本量从默认 8 放大到 50** 才暴露的 —— 默认规模下 hypothesis 抽不到这么大的页码。这正是「默认快、可环境变量放大」这套配置的价值所在。

### 缺陷与用例的对应关系（防止有人误删）

| 缺陷 | 钉住用例 | 形态 |
| --- | --- | --- |
| DEF-01 | `test_contract_export.py::test_live_422_body_conforms_to_declared_schema` | xfail strict |
| DEF-02a | `test_contract_export.py::test_spec_declares_bearer_auth_scheme` | xfail strict |
| DEF-02b | `test_contract_export.py::test_reachable_error_status_is_declared_in_spec` | xfail strict ×6 |
| DEF-03 | `test_contract_export.py::test_declared_form_login_returns_token` | xfail strict |
| DEF-04 | `test_handwritten_parity.py::test_auto_export_documents_declared_login_status` | xfail strict ×2 |
| DEF-05 | `test_schemathesis_properties.py::test_non_finite_decimal_literal_is_rejected_as_422` | **正常用例**（收口时已修，见 §10.1） |
| DEF-06 | `test_schemathesis_properties.py::test_out_of_range_pagination_is_rejected_not_500` | **正常用例**（收口时已修，见 §10.2） |

DEF-01/02a/02b/03/04 共 **11 条** `xfail(strict=True)`，全部带明确 reason 并指向本文件。**没有一条用 skip 掩盖。** 这 5 类都是「spec 的机器可读声明」与「实现/文档」之间的错配，修它们要动的是 FastAPI 的能力边界或导出策略，不属于本轮「关掉服务端崩溃」的范围，故保持原样。

---

## 7. 自检命令与原始输出

### ① `& $PY -m ruff check .`

```
All checks passed!
```

### ② `& $PY -m ruff format --check .`

```
75 files already formatted
```

### ③ `& $PY -m mypy`

```
Success: no issues found in 61 source files
```

（说明：改动前 mypy 是**红的** —— `apis\base.py:111:13: error: "Response" has no attribute "elapsed_ms"` 与 `:112:13 ... "was_retried"`。已在 691cb04 的独立 worktree 上跑 mypy 复现完全相同的两行，确认是既有基线债。由于 `apis/` 禁改，按任务书要求用 `[[tool.mypy.overrides]]` + `disable_error_code` 登记在 `pyproject.toml`（债 1/1b），未使用 `ignore_errors`。整合时请复核这条判断。）

### ④ `& $PY -m pytest -m api -n 4 --dist loadfile -q`

```
4 workers [92 items]
........................................................................ [ 78%]
....................                                                     [100%]
======================= 92 passed, 4 warnings in 12.21s =======================
```

（改造前基线 92 passed。**公网用例全部通过**，本机 `jsonplaceholder.typicode.com` 可达，无需单独重跑。）

### ⑤ `& $PY -m pytest -m mes -q`

```
================ 30 passed, 291 deselected, 1 warning in 8.03s ================
```

（改造前 30 passed。）

### ⑥ `& $PY -m pytest -m login -q`

```
================ 29 passed, 292 deselected, 1 warning in 2.43s ================
```

（改造前 29 passed。）

### ⑦ `& $PY -m pytest -m contract -q`

```
======== 203 passed, 106 deselected, 12 xfailed, 4 warnings in 18.07s =========
```

分文件耗时：

```
test_contract_export.py        -> 11 passed, 9 xfailed in 6.44s
test_handwritten_parity.py     -> 14 passed, 2 xfailed in 2.01s
test_schemathesis_properties.py-> 178 passed, 1 xfailed in 11.27s
```

（12 个 xfail 全部按预期 XFAIL，**没有 XPASS**。唯一 warning 是 `pylibs\schemathesis\generation\coverage.py:305` 的 `DeprecationWarning: jsonschema.exceptions.RefResolutionError is deprecated`，来自 schemathesis 自身，与本次改动无关。）

### ⑧ 导出脚本与漂移检测脚本各跑一次

```
$ & $PY scripts/export_openapi.py
[写出] ...\docs\openapi.json（80867 字节，MES Mock 服务（前缀 /api/v1））
[写出] ...\docs\openapi-login_app.json（2129 字节，登录演示服务（前缀 /api））
exit=0

$ & $PY scripts/export_openapi.py --check
[一致] ...\docs\openapi.json（80867 字节）
[一致] ...\docs\openapi-login_app.json（2129 字节）
exit=0
```

### ⑨ `& $PY -m pytest --collect-only -q | tail -1`

```
======================== 321 tests collected in 0.63s =========================
```

（改造前 106。增量 215 = contract 的 203 + 12 个 xfail 计数差异。）

---

## 8. 给调度者的整合清单

### 8.1 `.github/workflows/test.yml`（**必须加**，否则契约门禁形同虚设）

在跑测试的 job 里加一步漂移检测，建议插在 `pytest` 之前（先挡契约漂移，再跑用例）：

```yaml
      - name: 契约漂移检测（openapi.json 必须与代码同源）
        run: python scripts/export_openapi.py --check
```

若要跑契约属性套件，给 job 加环境变量与 marker（`contract` 会起本地 FastAPI + SQLite，无需公网）：

```yaml
      - name: 契约属性测试
        run: python -m pytest -m contract -q
        env:
          CONTRACT_MAX_EXAMPLES: "8"   # 默认值，CI 用；本地深挖改 50 或 CONTRACT_FULL=1
```

注意 CI 里必须能 import `schemathesis`（见 8.5）。

### 8.2 `README.md`（建议）

在「测试分层 / marker」一节加一行：

```markdown
- `pytest -m contract` — 契约驱动用例：校验 `docs/openapi.json` 与代码同源（漂移检测），
  并用 schemathesis 对真实 Mock 服务做属性驱动测试。要起本地 FastAPI + SQLite，无需公网。
  默认每个 operation 8 个 examples（约 11 秒）；`CONTRACT_MAX_EXAMPLES=50` 或 `CONTRACT_FULL=1` 放大。
```

在「接口契约」相关段落加（若原文档提到手写契约）：

```markdown
`docs/openapi.json` 与 `docs/openapi-login_app.json` 是从代码自动导出的契约产物（`scripts/export_openapi.py`）。
注意 `testcases/login/contract.py` 的手写契约描述的是 `login_app`（前缀 `/api`），
与 `mock_server`（前缀 `/api/v1`）是两个服务，对照结论见 `docs/wave1-contract-handoff.md`。
```

### 8.3 `CHANGELOG.md`（建议原文）

```markdown
### 新增
- 契约驱动测试：`scripts/export_openapi.py` 从 `app.openapi()` 自动导出
  `docs/openapi.json`（mock_server）与 `docs/openapi-login_app.json`（login_app），
  支持 `--check` 逐字节漂移检测作为契约 diff 门禁。
- 新增 `testcases/contract/`（marker `contract`，203 条用例）：schemathesis 属性驱动测试，
  覆盖正向生成符合 spec、负向生成被拒、Bearer 认证、权限矩阵逐格断言。
- 发现并钉住 5 类真实契约缺陷（DEF-01 ~ DEF-05），全部用 `xfail(strict=True)` 登记，
  详见 `docs/wave1-contract-handoff.md`。

### 变更
- `requirements.txt` 新增 `schemathesis==3.39.16`。
- `pyproject.toml`：登记 `apis.base` 的既有 mypy 基线债（`attr-defined`），修复 mypy 红灯。
```

### 8.4 `pyproject.toml`

我改动的只有 mypy overrides 段（债 1 拆分 + 债 1b）。`[tool.mypy] files` 已包含 `testcases` 与 `scripts`，`testcases/contract/` 自动被覆盖，**无需再改**。

### 8.5 新的 `requirements.lock`

需要重新生成（我按约束只改了 `requirements.txt`）：

```
pip-compile --output-file=requirements.lock --strip-extras requirements.txt
```

生成后请确认 lock 里出现 `schemathesis==3.39.16` 及其解析结果。实测（python 3.12.10）解析到的组合：

```
schemathesis==3.39.16
hypothesis==6.169.1
hypothesis-jsonschema==0.23.1
hypothesis-graphql==0.13.2
httpx==0.28.1
werkzeug==3.1.9
yarl==1.25.1
starlette-testclient==0.4.1
pytest-subtests==0.14.2
harfile==0.5.0
backoff==2.2.1
pyrate-limiter==3.9.0
junit-xml==1.9
tomli==2.5.0
tomli-w==1.2.0
click==8.1.8
colorama==0.4.6
```

**注意**：`schemathesis` 会拉进 `pytest<9` 与 `pytest-subtests`，与本仓库既有的 `pytest==8.3.3` 相容（实测无冲突），但 `pip-compile` 时请留意是否出现 `pytest` 版本被顶的情况。

### 8.6 其它

- `.gitattributes` 有 `*.json text eol=lf`（实测 `git check-attr text eol -- docs/openapi.json` → `text: set` / `eol: lf`），本机 `core.autocrlf = false`，文件按 LF + 末尾单换行写出（实测前 2000 字节里 CRLF 出现 0 次），`--check` 通过。**CI 侧仍有残余风险**：若 runner 的 `core.autocrlf=true`，checkout 时 json 会被转成 CRLF 而导出侧恒写 LF，`--check` 会误报漂移。建议整合时在 CI 里确认 `git config core.autocrlf`，必要时显式 `git config core.autocrlf false`。
- 没有改 `.gitignore`（`.hypothesis/`、`.pytest_cache/`、`__pycache__/` 都已被忽略）。

---

## 9. 未做到 / 不确定的地方

1. **`.github/workflows/test.yml` 未改**（任务书禁改），漂移门禁目前只在本地可跑。**这是我交付物里最需要整合者接手的一项**：门禁不进 CI 就等于没有。
2. **`README.md` / `CHANGELOG.md` 未改**（任务书禁改），建议原文见 §8。
3. **`requirements.lock` 未重建**（约束是「只准写进 requirements.txt」），见 §8.5。
4. **`pyproject.toml` 的 mypy 基线债登记是我的判断，请复核**。事实是：改动前 `mypy` 就报 `apis/base.py:111-112` 两行 `attr-defined`，在 691cb04 的独立 worktree 上可完全复现。我按任务书「债按现有 overrides 风格登记，不要 ignore_errors」加了一条 overrides。如果整合者认为「基线本来就不绿、不该由本次改动登记」，删掉 pyproject.toml 里债 1b 那段即可，届时 `mypy` 会回到 2 个错误（与本任务改动无关）。
5. **DEF-05 的「正确行为应该是 422」是我的推断**，不是服务端明文承诺的。依据是同一函数 `parse_qty` 对 `'abc'` 会回 422（同一文件 `quantity.py:46-51` 的意图明显是「非法数字 → ValidationFailed」）。**该推断已在波 1 收口时被采纳并落地为修复**（见 §10.1），不再是「记录现状」；如果整合者仍不认可，回退 `mock_server/quantity.py` 的那三处 `is_finite()` 判断即可，届时对应用例会从「断言 422」退回红灯。
6. **`testcases/contract/spec_contract.py` 的 `REQUIRED_ACTIONS`（26 条）与 `PERMISSIONS`（16 条）是我手工从 `mock_server/routers.py` 与 `security.py` 抄过来的**，不是自动推导的。有一条护栏用例 `test_every_operation_is_covered_by_the_fact_table` 断言 spec 的 operation 集合恰好等于 `REQUIRED_ACTIONS ∪ PUBLIC_OPERATIONS`，所以**新增端点会让它失败**（这是故意的 —— 提醒补表），但**权限矩阵本身改了不会被自动发现**。若要彻底自动化，可以把 `PERMISSIONS` 改成从 `mock_server.security` 直接 import（本轮没做，是为了让契约测试对被测服务的改动更敏感）。
7. **属性测试只覆盖了正向/负向生成的少量样本**（默认 8 个 examples），不是穷尽。默认值是按「CI 里不能失控」定的；深挖用 `CONTRACT_FULL=1`。
8. **没有做 stateful testing**（schemathesis 的 `get_stateful_tests` / links）。mock_server 有状态机（工单状态流转、幂等键、乐观锁），这一层用属性测试能挖出更多东西，本轮受时间边界限制未做。
9. **未验证在 Linux/CI 上的行为**。全部实测在 Windows + GBK 控制台 + Python 3.12.10 上完成。已知一个平台敏感点：`MockServer` 的 `DEFAULT_PYTHON` 指向 `.venv\Scripts\python.exe`（Windows 路径），在 `w1-contract` 下不存在会回落到 `sys.executable`（实测正常），但整合到原仓库后该路径存在，行为会切换成那个解释器 —— 在 CI 上请确认 `sys.executable` 与 `.venv` 是同一个环境。

---

## 10. 波 1 收口：把契约套件变成确定性门禁（追加节）

本节追加于 7333ba4 之后。**目标不是加功能，而是消灭「随机变红的门禁」**：默认 8 examples 时套件是绿的，但 hypothesis 每次抽的样本不同，样本量一旦放大（`CONTRACT_MAX_EXAMPLES=50`）就会因两条真实的服务端 500 而红。**会随机变红的门禁等于没有门禁**，所以本轮做了三件事：修掉那两处 500、删掉为它们写的豁免机制、把契约用例从「记录现状」改成「断言正确行为」。

### 10.1 DEF-05 修复：非有限十进制字面量 → 422（不再是 500）

**改的文件**：`mock_server/quantity.py`（三处函数：`parse_qty`、`parse_qty_allow_zero`、`parse_cent`）

**改法**：把 `quantize` 收进已有的 `try`，并显式挡掉非有限值。三处都从
```python
try:
    dec = Decimal(str(value))
except (InvalidOperation, ValueError) as exc:
    raise ValidationFailed(f"字段 {field} 不是合法数字: {value!r}") from exc
if dec != dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP):
```
改成
```python
try:
    dec = Decimal(str(value))
    if not dec.is_finite():
        raise ValidationFailed(f"字段 {field} 不是合法数字: {value!r}")
    quantized = dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
except (InvalidOperation, ValueError) as exc:
    raise ValidationFailed(f"字段 {field} 不是合法数字: {value!r}") from exc
if dec != quantized:
```
`parse_cent` 用 `Decimal("0.01")`、错误措辞是「不是合法金额」，其余同构。

**为什么加 `is_finite()` 这一句而不是只把 `quantize` 挪进 try**：`Decimal("NaN")` 的 `quantize` 会抛 `InvalidOperation`（能被 except 接住），但 `Decimal("NaN") != Decimal("NaN")` 恒为真，若不显式挡住，`NaN` 会一路走到「最多支持 3 位小数」这条**错误措辞**的分支上去 —— 状态码对、语义错。所以显式判定非有限值是必要的，不是冗余。实测覆盖 `INFINITY` / `-INFINITY` / `NaN` / `nan` / `1e999999`。

**实测对照（同一探针脚本，起真服务打 HTTP）**：

修前（`git stash` 掉两个服务端文件 → 纯 7333ba4）：
```
DEF-05 reverse qty=INFINITY
  -> 500 {"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"InvalidOperation"}}
DEF-05 issue qty=1e999999
  -> 500 {"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"InvalidOperation"}}
DEF-06 page huge
  -> 500 {"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"OverflowError"}}
```
修后：
```
DEF-05 reverse qty=INFINITY
  -> 422 {"code":"VALIDATION_ERROR","message":"字段 qty 不是合法数字: 'INFINITY'","detail":null}
DEF-05 issue qty=1e999999
  -> 422 {"code":"VALIDATION_ERROR","message":"字段 qty 不是合法数字: '1e999999'","detail":null}
DEF-06 page huge
  -> 400 {"code":"PAGINATION_OUT_OF_RANGE","message":"page 过大：page=461168601842738792 × page_size=20 超出可寻址范围，请改用更小的页码","detail":{"page":461168601842738792,"page_size":20,"max_offset":9223372036854775807}}
```

**契约用例随之改形态**（任务书要求：不许留 xfail 记录已修好的东西）：
- 删掉「取证 500」用例 `test_non_finite_decimal_literal_returns_500`。
- 把原来的 strict xfail `test_non_finite_decimal_literal_is_rejected_as_422` 改成**正常用例**，并加强断言：不只看状态码，还断言 `code == "VALIDATION_ERROR"` 且出错字段名出现在 message 里（只断言 422 的话，一个「缺字段」的无关 422 也能让它变绿）。参数从 5 组扩到 6 组（补 `NaN`）。
- 新增 `test_non_finite_decimal_parser_contract`：不经 HTTP，直接对三个解析函数遍历 5 个非有限字面量断言抛 `ValidationFailed(status=422)`，并断言 `parse_qty("1.500") == 1500` / `parse_qty_allow_zero("0") == 0` / `parse_cent("1.50") == 150` 不受影响（防止修法过宽把合法值也拒了）。**这条是为了防「只修了 `parse_qty` 一个函数」蒙混过关。**
- **删掉了整套 DEF-05 豁免机制**：`spec_contract.py` 里的 `NON_FINITE_LITERAL` 正则、`contains_non_finite_literal()`、`case_contains_non_finite_literal()` 全部删除（该文件不再 `import re`）；`test_schemathesis_properties.py` 负向用例里的 `known_defect` 记账分支也删除。现在正向与负向两条路径都是「任何 5xx 一律 `raise AssertionError`」，**没有任何宽泛的已知缺陷分类**。

### 10.2 DEF-06 修复：超大 `page` → 400（不再是 500）

**改的文件**：`mock_server/deps.py`

**改法**：新增具名常量，并在拼 offset 之前显式挡回：
```python
# SQLite 的 INTEGER 是有符号 64 位。OFFSET 一旦超过这个上限，sqlite3 会抛
# `OverflowError: Python int too large to convert to SQLite INTEGER`，
# 该异常不属于 BizError，会一路逃到 app.py 的兜底处理器变成 500 INTERNAL_ERROR。
# 所以 offset 必须在这一层就被挡成 400，而不是等数据库报错。
MAX_SQLITE_INTEGER = 2**63 - 1
```
```python
    # page 没有上界时，offset = (page - 1) * page_size 会溢出 SQLite 的 INTEGER，
    # 由数据库驱动抛出 OverflowError → 500。这里是纯输入范围问题，用 400 挡回去：
    # 与 page < 1 同类同码，前端拿到的一样是"你这个页码没法用"。
    offset = (page - 1) * page_size
    if offset > MAX_SQLITE_INTEGER:
        raise PaginationOutOfRange(
            f"page 过大：page={page} × page_size={page_size} 超出可寻址范围，请改用更小的页码",
            page=page,
            page_size=page_size,
            max_offset=MAX_SQLITE_INTEGER,
        )
```
`MAX_SQLITE_INTEGER` 加进了 `__all__`，供用例直接 import 钉边界。

**为什么用 400 + 复用既有 `PaginationOutOfRange`**：与既有的 `page < 1 → 400 PAGINATION_OUT_OF_RANGE` 同类同码，前端不需要为新情况写第二套分支。

**为什么没有给 Query 加 `le`/`ge` 上界（即没有动 spec）**：那会让 FastAPI 在进入依赖之前就把超界变成 422，直接破坏既有的 `page=0 → 400` 断言（`test_contract_export.py` 里已钉住），也会让导出的 `docs/openapi.json` 多出 `maximum` 字段从而触发漂移门禁。**`docs/openapi.json` 与 `docs/openapi-login_app.json` 本轮逐字节未改**（`--check` 输出 `[一致]` 即为证）。

**契约用例**：新增两条正常用例 ——
- `test_out_of_range_pagination_is_rejected_not_500`：4 组参数化（`page=461168601842738792`、`page=9223372036854775807`、`page=0`、`page_size=1000`），断言 400 且 code 正确。后两组是**回归护栏**，防止修上界时把既有的下界与 page_size 上界行为改坏。
- `test_pagination_boundary_is_where_sqlite_integer_ends`：不经 HTTP，直接 import `MAX_SQLITE_INTEGER` 与 `get_pagination`，钉住边界的**位置**（`MAX_SQLITE_INTEGER // page_size + 2` 必须拒、`page=1` 仍放行）。这样将来有人把界改小或改大都会被逮到。

### 10.3 顺带修掉的一处「假绿」（本轮自查发现，非任务书要求）

**问题**：补全视图给 operation 补 404 的规则原来是「有 path 占位符 就补」，但实测 `POST /api/v1/inventory/issue` 与 `POST /api/v1/inventory/reverse` 这两个**没有 path 占位符**的端点在请求体引用不存在的实体时也会回 404（`services.py:579` `issue_material` 的 `payload.material_code` 查不到物料、`services.py:586` 同函数的 `(material_code, warehouse)` 无库存记录、`services.py:635` `reverse_material` 同款）。于是 50 examples 下这两条正向用例因 `Received: 404` 而红（`acc2_50_run1`：`POST /api/v1/inventory/reverse`；`acc2_50_run2`：`POST /api/v1/inventory/issue` 与 `reverse` 各一条）。

**这是「我的测试视图不完整」，不是服务缺陷** —— 服务回 404 是对的（实体确实不存在），错的是补全视图没声明这两个端点会 404。

**修法（关键：必须精确，不能用字段名判定）**：新增显式端点表，逐条注明抛点出处：
```python
NOT_FOUND_FROM_BODY_REQUEST = frozenset(
    {
        "POST /api/v1/inventory/issue",
        "POST /api/v1/inventory/reverse",
    }
)
```
`build_test_view()` 的 404 条件从 `if has_path_param:` 改为 `if has_path_param or op_id in NOT_FOUND_FROM_BODY_REQUEST:`。

**为什么不能用「body 里有 material_code 就补 404」这种字段名规则**：`POST /api/v1/materials` 的 body 里同样有 `material_code`，但 `create_material` **只做重名校验**（400 `MATERIAL_CODE_DUPLICATE`），从不查存在性、**不会 404**。按字段名判定会给这个端点也发一张「通吃 404 的免死金牌」，恰好盖住真正该报的「本该 400/422 却回 404」这类回归。所以规则必须是**显式端点表**，而且要有用例钉住它没有多、没有少：
- `test_404_enrichment_set_equals_expected_signature`：断言补全视图里声明了 404 的端点集合**恰好等于**「有 path 占位符 ∪ `NOT_FOUND_FROM_BODY_REQUEST`」（多了少了都报错）。
- `test_body_sourced_404_table_excludes_create_material`：反例守卫，断言 `POST /api/v1/materials` **不在**表里，且表内容恰好是那两个端点。
- `test_body_sourced_404_endpoints_really_return_404`：起真服务对表里每个端点复现 404 + `code == "NOT_FOUND"`（证据，不是从代码读出来的推断）。
- `test_create_material_with_unknown_code_is_not_404`：反向行为证据，`POST /api/v1/materials` 传全新编码不会 404。

### 10.4 收口后的验收实测（全部在**当前字节**上重跑）

前置：每条命令前 `Remove-Item .hypothesis -Recurse -Force`；环境 `PYTHONPATH=<pylibs>`、`TMP/TEMP=<work>\_tmp`、`MAX_RESPONSE_MS=8000`；解释器 `C:\Users\lefan\DSH_project\DH_1\fullstack-test-framework\.venv\Scripts\python.exe`。

**验收 1 —— 默认样本量，连跑 3 次**
```
run1 exit=0 | ======== 213 passed, 130 deselected, 11 xfailed, 4 warnings in 24.73s =========
run2 exit=0 | ======== 213 passed, 130 deselected, 11 xfailed, 4 warnings in 24.37s =========
run3 exit=0 | ======== 213 passed, 130 deselected, 11 xfailed, 4 warnings in 23.99s =========
```

**验收 2 —— `CONTRACT_MAX_EXAMPLES=50`，连跑 2 次**
```
run1 exit=0 | ======== 213 passed, 130 deselected, 11 xfailed, 4 warnings in 58.71s =========
run2 exit=0 | === 213 passed, 130 deselected, 11 xfailed, 4 warnings in 60.99s (0:01:00) ====
```

**验收 3 —— 漂移门禁**
```
exit=0
[一致] C:\Users\lefan\DSH_project\接口自动化_3\work\w1-fixup\docs\openapi.json（80867 字节）
[一致] C:\Users\lefan\DSH_project\接口自动化_3\work\w1-fixup\docs\openapi-login_app.json（2129 字节）
```

**验收 4 —— `-m 'not ui'`**
```
exit=0
========= 329 passed, 14 deselected, 11 xfailed, 4 warnings in 57.13s =========
```

**验收 5 —— api / mes / login**
```
api:   exit=0 | ====================== 116 passed, 4 warnings in 18.48s =======================
mes:   exit=0 | =============== 54 passed, 300 deselected, 1 warning in 22.89s ================
login: exit=0 | ================ 29 passed, 325 deselected, 1 warning in 2.28s ================
```
（api 里打的公网 `https://jsonplaceholder.typicode.com` 本轮可达，116 全过，无需单独重跑证明。）

**验收 6 —— ruff / mypy**
```
ruff check:            exit=0  All checks passed!
ruff format --check:   exit=0  81 files already formatted
mypy:                  exit=0  Success: no issues found in 65 source files
```

**收集总数**：`354 tests collected in 0.89s`；同机的 7333ba4 基线为 `345 tests collected in 0.85s`（`git stash` 掉本节 4 个改动文件后实测），**净增 9 条** = 404 精确签名护栏 4 条 + DEF-05 的 parser 契约 1 条 + DEF-06 的边界位置 1 条 + DEF-05 参数化扩 1 组 + DEF-06 参数化 4 组 − 删掉的 DEF-05 取证 500 用例 1 条。另有 2 条用例由 xfail 转为正常。

**唯一 warning 恒为**（来自 schemathesis 自身，与本轮改动无关）：
```
C:\...\work\pylibs\schemathesis\generation\coverage.py:305: DeprecationWarning: jsonschema.exceptions.RefResolutionError is deprecated as of version 4.18.0.
```

**修前红 → 修后绿对照（同一命令 `CONTRACT_MAX_EXAMPLES=50 pytest -m contract -q`，均在删 `.hypothesis` 后）**

修前（`git stash` 掉全部 4 个改动文件 → 纯 7333ba4，即本轮任务书的起点）：
```
= 1 failed, 202 passed, 130 deselected, 12 xfailed, 4 warnings in 71.40s (0:01:11) =
FAILED testcases/contract/test_schemathesis_properties.py::test_positive_cases_conform_to_spec[GET /api/v1/work-orders]
```

该失败的报错原文（DEF-06）：
```
E       schemathesis.exceptions.CheckFailed:
E       1. Server error
E       2. Undocumented HTTP status code
E           Received: 500
E       [500] Internal Server Error:
E           `{"code":"INTERNAL_ERROR","message":"服务内部错误","detail":{"exception":"OverflowError"}}`
E           curl -X GET -H 'Authorization: [Filtered]' 'http://127.0.0.1:5780/api/v1/work-orders?page=461168601842738792'
testcases\contract\test_schemathesis_properties.py:178: CheckFailed
```

修前同一批文件下、DEF-05 的 xfail 原文（证明当时它确实是 500）：
```
XFAIL testcases/contract/test_schemathesis_properties.py::test_non_finite_decimal_literal_is_rejected_as_422 - DEF-05：mock_server/quantity.py:49 ... 正确行为应是 422（输入格式问题）。见 handoff「真实缺陷清单」DEF-05。
```

修后（当前字节，见验收 2）：`213 passed, 11 xfailed, 0 failed`。

**注意一个「非确定性」的事实**：修前连跑会红在不同端点/不同请求上 —— 一次是 `GET /api/v1/materials?page=461168601842738792`（`before_50.log`，OverflowError），一次是 `GET /api/v1/work-orders?page=461168601842738792`（`baseline_50_full.log`，同一 OverflowError），50 examples 中途的两轮则是 `POST /api/v1/inventory/issue` / `reverse` 的 404（`acc2_50_run1/2`，即 §10.3）。这正是「随机变红的门禁」的实证：**同一份代码，红在不同地方**。

### 10.5 本节改动的文件与提交范围

| 文件 | 改动 |
| --- | --- |
| `mock_server/quantity.py` | 三处解析函数加 `is_finite()` 判定 + 把 `quantize` 收进 `try`（DEF-05） |
| `mock_server/deps.py` | 新增 `MAX_SQLITE_INTEGER`，`get_pagination` 加 offset 上界 → 400 `PAGINATION_OUT_OF_RANGE`（DEF-06），并导出该常量 |
| `testcases/contract/spec_contract.py` | 404 补全规则从「有 path 占位符」扩为「∪ 显式端点表」；新增 `NOT_FOUND_FROM_BODY_REQUEST`；**删除**整套 DEF-05 豁免逻辑（`NON_FINITE_LITERAL` / `contains_non_finite_literal` / `case_contains_non_finite_literal`，不再 `import re`） |
| `testcases/contract/test_schemathesis_properties.py` | DEF-05 由「取证 500 + strict xfail」改为正常用例断言 422（并加 `test_non_finite_decimal_parser_contract`）；新增 DEF-06 两条用例；新增 404 精确签名护栏 4 条；**删除**负向用例里的 `known_defect` 豁免分支 |
| `docs/wave1-contract-handoff.md` | 本节；并同步修正 §6 的 DEF-05 描述与 §6 末的「缺陷与用例对应关系」表 |
| `docs/openapi.json`、`docs/openapi-login_app.json` | **未改**（`--check` 输出 `[一致]`，门禁仍有效） |

**保持不动的 strict xfail**：DEF-01、DEF-02a、DEF-02b(×6)、DEF-03、DEF-04(×2)，共 **11 条**。这 5 类都是 spec 的机器可读声明与实现/文档的错配，豁免依然成立。

### 10.6 本节未做到 / 不确定

1. **§10.3 的 404 端点表仍是手工维护的**。我用 4 条用例（含 2 条真服务复现）钉住了它**现在**是精确的，但如果将来 `services.py` 里新增一处「请求体引用实体查不到」的 404 抛点，补全视图不会自动跟上，会重新出现 50 examples 下的红色。彻底的做法是从 `services.py` 的抛点做静态分析或直接跑覆盖率探针，本轮没做。
2. **`MAX_SQLITE_INTEGER` 这个界的依据是「SQLite INTEGER 是有符号 64 位」**，未在 Windows 以外的 SQLite 构建上验证；也未验证「offset 恰好等于上限时确实能查询成功」（用例只钉了 `page=1` 放行与超界拒绝，未对边界值本身发起真查询 —— 因为那需要库里真有一个能寻址到那么远的表，构造成本高）。
3. **验收 2 只跑了 2 次 ×50 examples**。属性测试的绿灯是「这么多样本没找到反例」，不是证明。若要更高的确定性，需要 `CONTRACT_FULL=1` 或更长的 CI nightly。
4. **本轮没有改动任何 CI 文件**（任务书禁改）。§8.1 里「把 `python scripts/export_openapi.py --check` 加进 `.github/workflows/test.yml`」这条建议**依然待整合者落地**，且现在多了一条同样重要的待办：CI 跑 `-m contract` 时若想覆盖 DEF-06 这类「只有放大样本量才暴露」的缺陷，需要在 nightly 或专门的 job 里带 `CONTRACT_MAX_EXAMPLES=50`，默认 job 保持 8 以保证耗时可控（实测 8→50 是 24s→60s，约 2.5 倍）。
