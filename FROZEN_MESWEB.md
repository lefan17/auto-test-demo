# 批次3 冻结契约：本地 Demo Web + 混合链路

本文件由调度者冻结，**实现者不得修改**。它是「接口造数 → UI 走流程 → 接口校验」这条混合链路的
跨模块接缝：Demo Web 的 HTML/DOM、它自己的路由、它与 mock_server 的转发口径、以及测试侧的启动方式。
接缝两侧分别由「Demo Web 实现者」与「UI 用例实现者」依赖，任何一侧单方面改动都会让另一侧静默错位。

---

## 0. 为什么要有这个 Demo Web（背景，写进 docs 时用）

`data/config.yaml` 的 `base.ui` 是外网 `https://www.saucedemo.com`，它和本仓库自己的被测服务
（`mock_server`）没有任何业务关系。所以在它上面走不出「接口造数 → UI 操作 → 接口断言」这条链路：
UI 操作的是别人的购物车，接口断言的是我们自己的工单表，中间那条「同一份业务数据」的链条是断的。

`login_app` 也不行：它只有 3 个纯 JSON 端点（`/api/health`、`/api/login`、`/api/flaky`），
连一个 HTML 页面都没有（grep `text/html` / `<!DOCTYPE` 零命中）。

因此新建一个极简本地 Demo Web：**服务端渲染 HTML + 服务端转发到 mock_server**。
浏览器只和 Demo Web 同源通信，绕开 CORS——`mock_server` 刻意没有 CORS 中间件
（grep `allow_origins` / `add_middleware` 零命中），浏览器直连必然被拦。

---

## 1. 落点与目录归属（硬边界）

| 路径 | 归属 | 说明 |
|---|---|---|
| `mes_web/**` | **本批次新建** | Demo Web 应用（服务端）。包名必须是 `mes_web`，理由见 §2 |
| `testcases/ui/mes_web_ctl.py` | **本批次新建** | 启停 Demo Web 的测试侧控制模块 |
| `testcases/ui/test_mes_web_app.py` | **本批次新建** | Demo Web 自身的 HTTP 层用例（不起浏览器） |
| `ui/pages/mes_web_pages.py` | **本批次新建** | 新 Demo Web 的页面对象 |
| `testcases/ui/test_mes_mixed_flow.py` | **本批次新建** | 混合链路用例（起浏览器） |
| `docs/mixed-flow-usage.md` | **本批次新建** | 运行方式、边界、诚实声明 |
| `.gitignore` | 仅允许追加 | 忽略 Demo Web 的运行日志目录 |
| `pyproject.toml` | 仅允许在 `[tool.mypy] files` 追加 `"mes_web"` | 让 CI 的裸 `mypy` 真的检查它 |
| `pytest.ini` | **禁止修改** | 复用既有 `ui` marker，不新增 marker |
| `conftest.py`（根 / `testcases/ui/`） | **禁止修改** | 复用既有 `page`/`browser` fixture |
| `apis/**`、`common/**`、`mock_server/**`、`login_app/**`、`testcases/{api,mes,login,contract,platform}/**`、`qaplat/**` | **禁止改动** | 已验收资产 |
| `testcases/ui/**` 既有文件（conftest / test_inventory / test_login / artifacts） | **禁止改动** | 只能新增文件 |

---

## 2. 包名必须是 `mes_web`（不是 `web`、不是 `demo`）

根 `conftest.py:21-31` 把 `ROOT_DIR` 插到 `sys.path[0]`。仓库根下任何与标准库同名的目录都会
**遮蔽标准库**，而 pytest 自身启动就要 `import platform`——`platform/` 会让整套测试崩在收集阶段
（这是批次1 已经踩过并写进 `qaplat/FROZEN_CLI.md` 的坑）。`mes_web` 与标准库无冲突。

同理，**禁止**在仓库根新建 `web/`、`test/`、`types/`、`json/` 等与标准库或常见包同名的目录。

---

## 3. Demo Web 的 HTTP 契约（冻结）

`python -m mes_web.run --host 127.0.0.1 --port <N> --mock-base-url <URL>` 启动。
`mes_web/run.py` 必须支持 `argparse` 这三个参数；`--mock-base-url` 必填。

**`--mock-base-url` 指的是 mock_server 的 _根_ URL，不含 `/api/v1`**，即
`MockServer.root_url`（形如 `http://127.0.0.1:54321`），而**不是** `MockServer.base_url`
（后者已经带了 `/api/v1`）。`mes_web` 自己在拼接时补上 `/api/v1` 前缀。
两者混淆会让所有转发变成 `/api/v1/api/v1/...` 并 404——`§8.6` 那条证据就是用来抓这个的。

`GET /healthz` 回的 `mock_base_url` 必须是**收到的那个根 URL 原样回显**，供 `_wait_healthy` 核对。

### 3.1 对外路由（浏览器访问的就是这些）

| 方法 | 路径 | 行为 |
|---|---|---|
| `GET` | `/healthz` | 返回 `{"status": "ok", "mock_base_url": "<URL>"}`，供 `_wait_healthy` 探针用 |
| `GET` | `/` | 未登录跳 `302 → /login`；已登录跳 `302 → /work-orders` |
| `GET` | `/login` | 服务端渲染登录页 HTML |
| `POST` | `/login` | 表单 `username` / `password`（`application/x-www-form-urlencoded`）。成功 → `303 → /work-orders` 并 `Set-Cookie`；失败 → 200 重渲染登录页且页面内出现 `[data-test="error"]` |
| `GET` | `/logout` | 清除会话 → `303 → /login` |
| `GET` | `/work-orders` | 未登录跳 `/login`；已登录渲染工单列表 HTML |
| `POST` | `/work-orders/{order_no}/approve` | 服务端先读当前 `version` 再转发审批；成功后 `303 → /work-orders` |

### 3.2 会话实现（冻结：不引入任何新依赖）

**`itsdangerous` 未安装**（实测 `MISSING`），所以 **不能用** `starlette.middleware.sessions.SessionMiddleware`，
也不能用 `jinja2`（虽然本机 venv 里有 3.1.6，但它**不在 `requirements.txt`**——依赖未声明就意味着
CI 上没有）。两者都禁用。

冻结做法：
- **HTML 用纯字符串拼**（`str.format` / f-string），不引入模板引擎。
- **会话用「不透明随机 cookie + 进程内 dict」**：`secrets.token_urlsafe(32)` 作 cookie 值，
  `dict[str, Session]` 存在 app 实例上。
- 这是**刻意的 demo 级实现**，不是生产级：重启即失效、单进程、不支持多 worker。
  这个边界必须写进 `docs/mixed-flow-usage.md`，不得含糊成「实现了登录态」。

### 3.3 POST 表单：`python-multipart` 不可用（已实测，无分支）

调度者已实测：

```
multipart MISSING: No module named 'multipart'
python_multipart MISSING
```

`requirements.txt` 里也没有声明它（grep `multipart` 零命中）。因此：

- **禁止**用 FastAPI 的 `Form(...)` —— 它会在运行时抛「Form data requires python-multipart」。
- **禁止** pip install，**禁止**加进 `requirements.txt`（会污染 CI 依赖面）。
- **必须**这样实现：`POST /login` 里读 `body = await request.body()`，然后
  `urllib.parse.parse_qs(body.decode("utf-8"))` 自己解析出 `username` / `password`
  （`parse_qs` 的值是 list，取 `[0]`，并处理字段缺失）。

即路由签名里不能出现 `Form`，参数从原始 body 里取。

---

## 4. Demo Web → mock_server 的转发口径（冻结）

用 `requests`（**已声明的依赖**，见 `requirements.txt`）。**禁止用 httpx**（不在 venv 里，
需要 `PYTHONPATH=work\pylibs` 才能 import，属于环境特例，不能进产品代码）。

被转发的 mock_server 端点（前缀 `/api/v1`）：

| 用途 | 方法 + 路径 | 说明 |
|---|---|---|
| 登录 | `POST /api/v1/auth/login` | JSON `{"username": ..., "password": ...}`；响应含 `access_token` / `token_type`，实测还有 `expires_in` 与 `user`（只用 `access_token`） |
| 当前用户 | `GET /api/v1/auth/me` | `Authorization: Bearer <token>` |
| 工单列表 | `GET /api/v1/work-orders` | 响应信封见 §4.1 |
| 工单详情 | `GET /api/v1/work-orders/{order_no}` | 取 `version` 用于乐观锁 |
| 审批 | `POST /api/v1/work-orders/{order_no}/approve` | JSON `{"version": <int>}` |

**账号来源必须是单一真值**：`from common.secrets import role_password, role_username`，
不要自己写账号名或口令。可用角色：`ADMIN` / `PLANNER` / `OPERATOR` / `OPERATOR2` / `QA`。
审批权限是 `ADMIN`/`PLANNER`，所以混合链路用 **`PLANNER`**。

### 4.1 列表响应信封（**不是** FastAPI 默认的直接数组）

实测 `mock_server/schemas.py` 的工单列表模型是：

```
{"items": [WorkOrderOut, ...], "total": int, "page": int, "page_size": int, "pages": int}
```

`WorkOrderOut` 关键字段：`order_no`、`product_code`、`product_name`、`planned_qty`、`status`、
`status_text`、`priority`、`owner`、`version`。

### 4.2 状态机（混合链路只走第一步）

`mock_server/services.py:54-55` 的 `ALLOWED_TRANSITIONS`：`PENDING → APPROVED | CANCELLED`，
`APPROVED → SCHEDULED | CANCELLED`。新建工单初始状态是 `PENDING`。
混合链路**只做 `PENDING → APPROVED`**，不往排产/报工延伸。

---

## 5. DOM 契约（冻结）

页面对象与混合用例都依赖这些 `data-test` 属性。**两侧各自独立依赖它，所以属性名不许改**。

### 登录页

| 属性 | 元素 |
|---|---|
| `[data-test="username"]` | `<input name="username">` |
| `[data-test="password"]` | `<input name="password">` |
| `[data-test="login-button"]` | 提交按钮（`type="submit"`） |
| `[data-test="error"]` | 登录失败的提示容器（成功时不渲染此元素） |

### 工单列表页

| 属性 | 元素 |
|---|---|
| `[data-test="page-title"]` | 页面标题容器 |
| `[data-test="work-order-row"]` | 每一行工单（列表容器内可多次出现） |
| `[data-test="order-no"]` | 行内单号文本（**必须是纯文本，不含子元素包裹**） |
| `[data-test="product-name"]` | 行内产品名文本 |
| `[data-test="status"]` | 行内状态文本（值是 `status`，如 `PENDING` / `APPROVED`） |
| `[data-test="approve-button"]` | 行内审批按钮（`status == "PENDING"` 时才渲染；已是 `APPROVED` 则渲染 `[data-test="status"]` 文本且无按钮） |
| `[data-test="logout-button"]` | 退出登录按钮 |

**行内定位约定**：页面对象必须支持「按单号找到那一行，再在该行内定位子元素」，
所以 `order-no` 的文本必须严格等于 `order_no` 本身，不能带前缀（如 `单号：WO-xxx`）。

---

## 6. 测试侧启动契约（冻结）

### 6.1 `testcases/ui/mes_web_ctl.py`

```python
class MesWebError(RuntimeError): ...


def free_port() -> int:
    """从 mock_server_ctl 重新导出，不要重复实现。"""


class MesWebServer:
    def __init__(
        self,
        mock_base_url: str,
        port: int | None = None,
        log_path: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None: ...
    def start(self, timeout: float = 30.0) -> "MesWebServer": ...
    def stop(self) -> None: ...
    @property
    def base_url(self) -> str: ...
```

必须照抄 `testcases/mes/mock_server_ctl.py` 已经验证过的四个决定（**不要重新发明**）：

1. **解释器解析**：`os.getenv("MES_WEB_PYTHON") or (str(DEFAULT_PYTHON) if DEFAULT_PYTHON.exists() else sys.executable)`，
   其中 `DEFAULT_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"`。
   （本机这个 venv **不存在**，会自动回落到 `sys.executable`，这正是想要的行为；CI 上是 Linux，同样走回落。）
2. **端口由 OS 分配**：`free_port()` 用 socket 绑 0 取端口。**禁止写死端口**——上次跑崩留下的
   进程占着端口会让新一次直接起不来，报错还是 `address already in use`，容易误判成代码坏了。
3. **日志写文件而不是 `PIPE`**：子进程写满管道缓冲区会卡死。
4. **`_wait_healthy` 必须核对 `/healthz` 返回的 `mock_base_url` 与期望一致**——防「探针 200
   但转发到了错的后端，跑在错数据上还全绿」。

### 6.2 混合用例的两个服务从哪来

`testcases/ui/` **看不到** `testcases/mes/conftest.py` 的 `mes_server` fixture
（pytest conftest 只对自己的目录及以下生效），所以在混合用例里自建 fixture。
**不要去改任何 conftest。**

两个 import 的**形式不同**，这一点已实测，写错必然 `ModuleNotFoundError`：

```python
from mock_server_ctl import MockServer  # 裸名（正确）
from testcases.ui.mes_web_ctl import MesWebServer  # 带包名（正确）
```

原因（实测复刻布局验证过）：
- `testcases/mes/` **没有** `__init__.py`，而根 `conftest.py:27-31` 把 `MES_DIR` 插进了
  `sys.path`，所以 `mock_server_ctl` 是顶层模块，**裸名可行**。
- `testcases/ui/` **有** `__init__.py`（`testcases/` 也有），而根 `conftest.py` 插的是
  `LOGIN_DIR` 与 `MES_DIR`、**没有** `UI_DIR`。于是 `testcases/ui/mes_web_ctl.py` 只能作为
  包内模块导入，**裸名 `import mes_web_ctl` 会 `ModuleNotFoundError`**。

实测证据（复刻同构布局，`testcases/`+`testcases/ui/` 有 `__init__.py`、`testcases/mes/` 无）：
`test_bare.py`（裸名）FAILED `No module named 'mes_web_ctl'`，
`test_dotted.py`（`from testcases.ui import mes_web_ctl`）PASSED，
`test_mes_bare.py`（裸名，在 `testcases/mes/` 下）PASSED。

### 6.3 浏览器 fixture 复用既有实现

`testcases/ui/conftest.py` 已有 `browser`（session 级）、`browser_context`（function 级）、
`page`（function 级），并带失败截图钩子（`testcases/ui/artifacts/{item.name}.png`）。
混合用例**直接复用这些**，不要自己再写一套。

`testcases/ui/test_mes_web_app.py` 不申请 `page`，因此**不会触发 chromium 启动**。

---

## 7. marker 与 CI 影响（冻结）

- 新用例只打 `pytest.mark.ui`（可再加 `regression`）。**不新增 marker**，所以 `pytest.ini` 不动。
- 于是：`pytest -m "not ui"` 的结果**必须一条不变**——`passed` 与 `xfailed` 是硬指标
  （基线 `437 passed, 11 xfailed`）；`deselected` 会从 14 变成 34，因为新增的 20 条 ui 用例
  被这条 `-m` 表达式排除了，这是**预期变化**，不是回归。
- 新用例进入 CI 的 `test` job 里 `pytest -m ui` 那一步（`.github/workflows/test.yml:299`，
  该步 `continue-on-error: true`，且前面已 `playwright install chromium --with-deps`）。
- CI 的 lint job 跑 **`ruff format --check .`** 和 `ruff check .`（全仓），typecheck job 跑
  **裸 `python -m mypy`**（无参数，读 `pyproject.toml` 的 `[tool.mypy] files`）。
  三条都必须 `exit=0`。

---

## 8. 必须交付的证据（缺一条即不算完成）

命令一律在仓库根目录、Python 解释器
`C:\Users\lefan\DSH_project\DH_1\fullstack-test-framework\.venv\Scripts\python.exe` 下执行，
环境变量 `PYTHONPATH=C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs`、
`TMP`/`TEMP=C:\Users\lefan\DSH_project\接口自动化_3\work\pw-tmp`。

1. **混合链路真绿**：`pytest -m ui -q` 的原始尾部输出（须含新用例，且既有 14 条不受影响）。
2. **可证伪实验（最重要）**：故意把混合链路的某一环改坏（例如断言一个不存在的 `order_no`，
   或让 Demo Web 转发到错的后端），**贴出变红的原始输出**，然后改回并贴出再次变绿。
   一个从没红过的用例无法证明它在测东西。必须实测，不许口述。
3. **`pytest -m "not ui" -q`** → `437 passed` 与 `11 xfailed` 必须一条不变；
   `deselected` 从 14 变为 34 属预期（新增 20 条 ui 用例被排除）。贴尾部。
4. **`ruff format --check .`**、**`ruff check .`**、**裸 `python -m mypy`** 三条的尾部 + `exit=0`。
5. **§3.3 的实测结论**：`python-multipart` 到底可用不可用，选了哪条路径。
6. **前端转发到错后端会被抓住**的证据：把 `--mock-base-url` 指向一个不存在的端口，
   贴出 `_wait_healthy` 报出的真实错误文本。
7. **诚实清单**：明确列出「没验证到的部分」（例如 CI 上 Linux 路径未实测、多 worker 会话未验证、
   真实浏览器版本差异未覆盖等）。

## 9. 禁止事项

- 禁止引入任何新依赖（不改 `requirements.txt`）。
- 禁止使用 `jinja2`、`itsdangerous`、`httpx`、`python-multipart`（除非 §3.3 实测可用且已声明）。
- 禁止改动 §1 表中「禁止改动」的任何文件。
- 禁止为了让用例变绿而放宽断言、加 `pytest.skip`、加 `xfail`、或把断言改成 `assert True`。
  跑不通就如实报告跑不通，并给出原始报错。
- 禁止写死端口、禁止写死 `order_no`（工单号由 `DataFactory`/接口生成）。
