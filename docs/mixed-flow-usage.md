# 混合链路：接口造数 → UI 走流程 → 接口校验

本文说明 `mes_web`（Demo Web）+ 页面对象 + 混合用例的使用方式、架构与边界。
契约见 `FROZEN_MESWEB.md`（冻结，不可改）；本文只讲「怎么用」和「做到哪为止」。

## 1. 它解决什么问题

仓库原有的 UI 层（`testcases/ui/test_login.py`、`test_inventory.py`）打的是 `data/config.yaml`
里的 `base.ui`——外网 saucedemo 练习站。它和本仓库的被测服务 `mock_server` 没有任何业务关系：
在它上面点按钮产生不了 `mock_server` 里的数据，于是「UI 操作」和「接口断言」操作的不是同一份东西，
**链路是断的**——UI 用例只能自证渲染，接口用例只能自证契约，中间那一段没人管。

补法不是给 saucedemo 加断言，而是加一个同源的服务端渲染站点，让浏览器操作真正落到自己的后端上：

```
 ①接口造数                ②UI 走流程                    ③接口校验
 MesApi.create_work_order 浏览器点「审批」              MesApi.work_order
        │                       │                             │
        ▼                       ▼                             ▼
 ┌─────────────┐  requests  ┌─────────────┐  浏览器   ┌──────────────┐
 │ mock_server │◄───────────│  mes_web    │◄─────────│  playwright  │
 │  (被测服务) │  转发+渲染 │ (Demo Web)  │  同源    │   chromium   │
 └─────────────┘            └─────────────┘           └──────────────┘
```

为什么必须加中间这一层：`mock_server` 刻意**没有 CORS 中间件**，浏览器直连它的跨源请求会被拦。
让浏览器只和 `mes_web` 同源通信、由 `mes_web` 用 `requests` 在服务端转发，就不存在跨源问题了。

链路两端各用一次接口（①和③）是刻意的：UI 只负责「走流程」这一段，状态对不对由接口独立取证。
UI 断言不能自证——Demo Web 渲染错了、后端状态错了，都可能让「只看 UI」的断言依然绿。

## 2. 怎么跑

### 2.1 环境变量（必须设）

Playwright 启动需要可写的临时目录，否则报 `EPERM mkdtemp`；`pylibs` 是本机依赖所在。

```powershell
chcp 65001 > $null
$env:PYTHONPATH="C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs"
$env:TMP="C:\Users\lefan\DSH_project\接口自动化_3\work\pw-tmp"
$env:TEMP=$env:TMP
$env:PYTHONIOENCODING="utf-8"
```

解释器（本仓库根的 `.venv` 不存在，控制器会自动回落到 `sys.executable`）：

```
C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe
```

### 2.2 跑用例

```powershell
cd C:\Users\lefan\DSH_project\接口自动化_3\work\auto-test-demo

# 只跑本次新增的混合链路 + Demo Web HTTP 层
python -m pytest testcases/ui/test_mes_mixed_flow.py testcases/ui/test_mes_web_app.py -q

# 整个 UI 层（既有 saucedemo 用例 + 本次新增）
python -m pytest -m ui -q
```

两个服务都是 fixture 自建的，**不需要预先手工起任何进程**：

| fixture | scope | 起什么 |
| --- | --- | --- |
| `http_mock_server` / `mixed_mock_server` | session | `MockServer`（临时目录里的独立 sqlite 库） |
| `http_mes_web` / `mixed_mes_web` | session | `MesWebServer`（端口由 OS 分配，日志写文件） |

`testcases/ui/test_mes_web_app.py` **不申请 `page` fixture**，因此不起 chromium——它是 HTTP 层的快速反馈。

### 2.3 单独手工起 Demo Web（排查用）

```powershell
python -m mes_web.run --host 127.0.0.1 --port 8000 --mock-base-url http://127.0.0.1:<mock端口>
```

- `--mock-base-url` **必填**，且必须是 mock_server 的**根** URL（**不含** `/api/v1`）。
  传成含 `/api/v1` 的地址会让转发变成 `/api/v1/api/v1/...` 全 404。
  拿不到端口时可以先起 mock_server：`python -m mock_server.run --help`。
- 起好后打开 `http://127.0.0.1:8000/login`，用 mock_server 的账号登录（见 `common/secrets.py`，
  审批需要 `PLANNER` 或 `ADMIN` 角色）。
- `GET /healthz` 回显 `{"status":"ok","mock_base_url":"<收到的根URL>"}`，可用来确认它连的是哪个后端。

## 3. 文件与职责

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `mes_web/__init__.py` | 9 | 包声明。包名必须是 `mes_web`（契约 §2：`platform/` 之类的名字会在 pytest 收集阶段撞标准库） |
| `mes_web/app.py` | 344 | FastAPI 应用：路由、会话、转发、服务端渲染（纯字符串拼，无模板引擎） |
| `mes_web/run.py` | 50 | `argparse` 起 uvicorn；`--host` / `--port` / `--mock-base-url`（必填） |
| `testcases/ui/mes_web_ctl.py` | 207 | `MesWebServer` 启停控制：解释器解析、`free_port`、日志落文件、`_wait_healthy` |
| `testcases/ui/test_mes_web_app.py` | 330 | Demo Web 自身 HTTP 层用例（15 条，不起浏览器） |
| `ui/pages/mes_web_pages.py` | 140 | 页面对象：`MesLoginPage` / `WorkOrdersPage` |
| `testcases/ui/test_mes_mixed_flow.py` | 195 | 混合链路用例（5 条，用浏览器） |
| `docs/mixed-flow-usage.md` | 151 | 本文 |

设计上的两个要点：

- **DOM 定位器只写在页面对象里。** 契约 §5 冻结了 `data-test` 属性名，两侧（Demo Web 与用例）
  各自独立依赖它。改名只改 `ui/pages/mes_web_pages.py`。
- **按单号找行用带锚点的正则**（`WorkOrdersPage.row_by_order_no`），不是子串匹配。
  契约要求 `[data-test="order-no"]` 的文本**严格等于** `order_no`，正是为了让行内定位能精确。
  子串匹配在单号互为前缀时（`WO-1` / `WO-10`）会静默挑错行——这类错最难查。

## 4. 边界（这些都是有意为之，不是待办）

Demo Web 是**demo 级实现**，契约 §3.2 冻结了这个定位。以下边界是有意选择的结果：

1. **会话是「进程内 dict + 不透明 cookie」**，不是签名会话。
   代价：**重启即失效**（服务重启后所有登录态丢失，页面会弹回登录页）；
   **单进程**（多 worker / 多副本时，A 进程发的 cookie 到 B 进程查不到，表现为随机被登出）；
   **不支持多 worker**——所以 `mes_web.run` 没有 `--workers` 参数，也不要加。
   为什么这样做：`starlette.middleware.sessions.SessionMiddleware` 依赖 `itsdangerous`，实测未安装，
   而任务禁止引入新依赖。
2. **HTML 是纯字符串拼**，没有模板引擎。`jinja2` 本机虽有，但不在 `requirements.txt` 里，
   CI 上不存在——依赖它会让「本机能跑」变成假象。所有插入值都过 `html.escape`。
3. **`POST /login` 不能用 `Form(...)`**：`python-multipart` 未安装（实测 `No module named 'multipart'`），
   用了会在运行时抛「Form data requires python-multipart」。所以自己 `await request.body()`
   + `urllib.parse.parse_qs` 解析（契约 §3.3）。
4. **转发用 `requests`**（已声明），不用 `httpx`（未声明）。
5. **列表只显示第一页**（`page_size=100`，取服务端上限）。工单数超过 100 时列表页看不全——
   混合用例造的工单数量远低于这个上限，但这是一个真实的能力边界。
6. **账号不写死**：口令由人在登录页输入，`mes_web/app.py` 里一个账号字面量都没有。

## 5. 失败时的表现（刻意设计）

- 转发到上游失败（非 200）时，Demo Web **不假装成功**：渲染一张诊断页并返回 **502**，
  把上游状态码和响应体原文放进 `[data-test="forward-error"]`。
  如果这里静默跳回列表页，用例会看到「状态没变」而在错误的地方找原因。
- 上游返回 401/403（令牌在 mock_server 侧失效，例如它重启了）：清掉本地会话并 302 回 `/login`。
  不这样做的话，401 会被渲染成一张空列表——看起来像「真的一单都没有」。
- `_wait_healthy` 除了探 `/healthz`，还会**核对它回显的 `mock_base_url`**，
  并额外探一次后端自己的 `/healthz`。原因是 Demo Web 的 `/healthz` 是纯回显：
  只探它的话，`--mock-base-url` 指向一个根本没有服务的端口也会「健康」，
  而这类错配在下游表现为「列表是空的」，很容易被当成业务问题查半天。

## 6. 已知未覆盖（诚实清单）

- **CI（Linux）路径未实测**：本文所有命令都在本机 Windows 上跑过。
  `mes_web_ctl.py` 的 `DEFAULT_PYTHON` 是 `Scripts/python.exe`（Windows 布局），
  Linux CI 上该路径不存在，会回落到 `sys.executable`——回落逻辑与 `mock_server_ctl.py` 一致，
  但**没有在 Linux 上真跑过一次**。
- **多 worker 会话未验证**：按 §4.1 的边界，多 worker 本来就不被支持，所以没有对应用例，
  也没有实测过「多 worker 下会怎样失效」的现场。
- **并发审批未验证**：混合链路只走 `PENDING → APPROVED` 单线程路径。
  两人同时点同一单的竞态（乐观锁 `version` 冲突返回 409）没有 UI 层用例。
- **`CANCELLED` 分支未覆盖**：契约 §4.2 的 `PENDING → CANCELLED` 混合链路不做，页面上没有取消按钮。
- **浏览器仅 chromium**：`browser` fixture 是 chromium headless（既有 fixture，本次复用）。
  firefox / webkit 未跑。
