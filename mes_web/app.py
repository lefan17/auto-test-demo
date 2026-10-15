"""Demo Web：服务端渲染 HTML + 服务端转发到 mock_server（FROZEN_MESWEB.md §3–§5）。

为什么要有它：`data/config.yaml` 的 `base.ui` 是外网 saucedemo，和本仓库自己的被测服务
（`mock_server`）没有任何业务关系，在它上面走不出「接口造数 → UI 操作 → 接口断言」。
而 `mock_server` 刻意没有 CORS 中间件（浏览器直连必然被拦），所以中间加一层同源的服务端渲染页面，
浏览器只和这一层说话，由这一层用 `requests` 转发——同源就不存在 CORS 问题。

刻意做成 demo 级实现（契约 §3.2 冻结，不是能力缺失，是不要新依赖）：
- HTML 用纯字符串拼，不用模板引擎（`jinja2` 本机有但不在 `requirements.txt` 里，CI 上就没有）；
- 会话是「不透明随机 cookie + 进程内 dict」，不用 `SessionMiddleware`
  （它依赖 `itsdangerous`，实测未安装）。代价是重启即失效、单进程、不支持多 worker。

被转发的账号不在这里写死：口令由人在登录页输入，本模块一个账号字面量都没有。
"""

from __future__ import annotations

import html
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

API_PREFIX = "/api/v1"
COOKIE_NAME = "mes_web_session"
# 取到服务端允许的最大页长（mock_server/deps.py 的 MAX_PAGE_SIZE=100）：
# 列表只显示第一页，页长不给够的话「刚造的工单」可能落在第二页，UI 上就找不到它。
LIST_PAGE_SIZE = 100
# 服务端调用 mock_server 的超时。UI 用例的默认等待是秒级，这里必须更短，
# 否则 mock_server 挂掉时用例表现为「卡住」而不是「明确报错」。
FORWARD_TIMEOUT = 10.0


@dataclass
class Session:
    """登录态：只放转发要用的令牌和展示用的用户名。

    不做校验、不做签名——cookie 值本身就是 `secrets.token_urlsafe(32)` 的不可猜随机串，
    它是「服务端字典的键」。这是 demo 级会话的边界，见 docs/mixed-flow-usage.md。
    """

    token: str
    username: str


# --------------------------------------------------------------------------
# HTML 渲染（纯字符串拼；所有插入值都过 html.escape，页面显示的是用户/接口来的数据）
# --------------------------------------------------------------------------
def render_login_page(error: str | None = None) -> str:
    """登录页。`[data-test="error"]` 只在失败时渲染，成功时不出现（契约 §5）。"""
    error_html = f'<p data-test="error">{html.escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>登录 · MES Demo Web</title>
</head>
<body>
<h1 data-test="page-title">MES Demo Web 登录</h1>
{error_html}
<form method="post" action="/login">
  <p><label>用户名 <input data-test="username" name="username" type="text"></label></p>
  <p><label>口令 <input data-test="password" name="password" type="password"></label></p>
  <p><button data-test="login-button" type="submit">登录</button></p>
</form>
</body>
</html>
"""


def _render_row(order: dict[str, Any]) -> str:
    """一行工单。

    `[data-test="order-no"]` 必须是**纯文本**且严格等于 `order_no`：页面对象要靠
    「行内单号精确匹配」找行，带前缀（`单号：WO-x`）就没法用文本定位了。
    `[data-test="approve-button"]` 只在 `status == "PENDING"` 时渲染（契约 §5）。
    """
    order_no = str(order.get("order_no", ""))
    status = str(order.get("status", ""))
    approve_html = ""
    if status == "PENDING":
        action = urllib.parse.quote(order_no, safe="")
        approve_html = (
            f'<form method="post" action="/work-orders/{html.escape(action, quote=True)}/approve">'
            f'<button data-test="approve-button" type="submit">审批</button></form>'
        )
    return f"""<tr data-test="work-order-row">
<td data-test="order-no">{html.escape(order_no)}</td>
<td data-test="product-name">{html.escape(str(order.get("product_name", "")))}</td>
<td data-test="product-code">{html.escape(str(order.get("product_code", "")))}</td>
<td data-test="planned-qty">{html.escape(str(order.get("planned_qty", "")))}</td>
<td data-test="status">{html.escape(status)}</td>
<td>{approve_html}</td>
</tr>"""


def render_work_orders_page(orders: list[dict[str, Any]], username: str) -> str:
    """工单列表页（服务端渲染，数据来自 mock_server 的列表信封）。"""
    rows = "\n".join(_render_row(order) for order in orders)
    empty_html = "<p>暂无工单</p>" if not orders else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>工单列表 · MES Demo Web</title>
</head>
<body>
<h1 data-test="page-title">工单列表</h1>
<p>当前用户：{html.escape(username)}　共 {len(orders)} 条（首页）</p>
<p><a data-test="logout-button" href="/logout">退出登录</a></p>
{empty_html}
<table border="1">
<thead>
<tr><th>单号</th><th>产品</th><th>编码</th><th>计划数量</th><th>状态</th><th>操作</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""


def render_error_page(title: str, detail: str) -> str:
    """转发失败时的诊断页。

    契约只规定了成功路径（303 → /work-orders），没规定失败怎么办。这里是刻意选择：
    **不静默**——把上游状态码和响应体原文渲染出来，让「转发口径错」这类问题一眼可见，
    而不是伪装成一次正常的跳转、让人去猜为什么状态没变。
    """
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>转发失败 · MES Demo Web</title>
</head>
<body>
<h1 data-test="page-title">{html.escape(title)}</h1>
<pre data-test="forward-error">{html.escape(detail)}</pre>
<p><a href="/work-orders">返回工单列表</a></p>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 转发到 mock_server（同步 requests；在 async 路由里用 threadpool 调用，不阻塞事件循环）
# --------------------------------------------------------------------------
def _api_url(mock_base_url: str, path: str) -> str:
    """`--mock-base-url` 是 mock_server 的**根** URL（不含 `/api/v1`），前缀在这里补。"""
    return f"{mock_base_url.rstrip('/')}{API_PREFIX}{path}"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _json_or_empty(resp: requests.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _forward_login(mock_base_url: str, username: str, password: str) -> tuple[int, dict[str, Any]]:
    resp = requests.post(
        _api_url(mock_base_url, "/auth/login"),
        json={"username": username, "password": password},
        timeout=FORWARD_TIMEOUT,
    )
    return resp.status_code, _json_or_empty(resp)


def _forward_list(mock_base_url: str, token: str) -> tuple[int, dict[str, Any]]:
    resp = requests.get(
        _api_url(mock_base_url, "/work-orders"),
        headers=_auth_headers(token),
        params={"page_size": LIST_PAGE_SIZE},
        timeout=FORWARD_TIMEOUT,
    )
    return resp.status_code, _json_or_empty(resp)


def _forward_detail(mock_base_url: str, token: str, order_no: str) -> tuple[int, dict[str, Any]]:
    order_path = urllib.parse.quote(order_no, safe="")
    resp = requests.get(
        _api_url(mock_base_url, f"/work-orders/{order_path}"),
        headers=_auth_headers(token),
        timeout=FORWARD_TIMEOUT,
    )
    return resp.status_code, _json_or_empty(resp)


def _forward_approve(mock_base_url: str, token: str, order_no: str, version: int) -> tuple[int, str]:
    order_path = urllib.parse.quote(order_no, safe="")
    resp = requests.post(
        _api_url(mock_base_url, f"/work-orders/{order_path}/approve"),
        headers=_auth_headers(token),
        json={"version": version},
        timeout=FORWARD_TIMEOUT,
    )
    return resp.status_code, resp.text


# --------------------------------------------------------------------------
# 应用
# --------------------------------------------------------------------------
def create_app(mock_base_url: str) -> FastAPI:
    """建应用。`mock_base_url` 必须是 mock_server 的根 URL，原样存进 app.state 供 /healthz 回显。"""
    app = FastAPI(title="MES Demo Web", docs_url=None, redoc_url=None)
    app.state.mock_base_url = mock_base_url
    # 会话表挂在 app 实例上：单进程、重启即失效（契约 §3.2 冻结的 demo 级实现）。
    app.state.sessions = {}

    def sessions() -> dict[str, Session]:
        store: dict[str, Session] = app.state.sessions
        return store

    def current_session(request: Request) -> Session | None:
        sid = request.cookies.get(COOKIE_NAME)
        return sessions().get(sid) if sid else None

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=302)

    def see_other(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def upstream_error(what: str, status_code: int, detail: str) -> HTMLResponse:
        return HTMLResponse(
            render_error_page(f"{what}失败", f"mock_server 返回 HTTP {status_code}\n{detail}"),
            status_code=502,
        )

    def unreachable(what: str, exc: Exception) -> HTMLResponse:
        return HTMLResponse(
            render_error_page(f"{what}失败", f"无法连接 mock_server（{mock_base_url}）：{exc!r}"),
            status_code=502,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """探针：把收到的根 URL **原样**回显，供 `_wait_healthy` 核对连的是哪个后端。"""
        return {"status": "ok", "mock_base_url": app.state.mock_base_url}

    @app.get("/")
    def index(request: Request) -> Response:
        # 契约 §3.1 冻结 `/` 的两个分支都是 302（表单跳转才是 303，见 see_other）。
        # 这里是「换个地方看」而不是「POST 之后的重定向」，所以用 302。
        target = "/work-orders" if current_session(request) else "/login"
        return RedirectResponse(target, status_code=302)

    @app.get("/login")
    def login_form() -> HTMLResponse:
        return HTMLResponse(render_login_page())

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        """表单登录。

        这里**不能**用 `Form(...)`：`python-multipart` 未安装（实测 `No module named 'multipart'`），
        用了会在运行时抛「Form data requires python-multipart」。所以自己读原始 body 解析
        （契约 §3.3 冻结），`parse_qs` 的值是 list，取 `[0]` 并处理字段缺失。
        """
        raw = await request.body()
        fields = urllib.parse.parse_qs(raw.decode("utf-8"))
        username = (fields.get("username") or [""])[0]
        password = (fields.get("password") or [""])[0]

        try:
            status_code, body = await run_in_threadpool(_forward_login, mock_base_url, username, password)
        except requests.RequestException as exc:
            return HTMLResponse(render_login_page(f"无法连接 mock_server：{exc}"), status_code=200)

        if status_code != 200 or not body.get("access_token"):
            return HTMLResponse(render_login_page("用户名或口令不正确"), status_code=200)

        sid = secrets.token_urlsafe(32)
        sessions()[sid] = Session(token=str(body["access_token"]), username=username)
        response = see_other("/work-orders")
        response.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/logout")
    def logout(request: Request) -> RedirectResponse:
        sid = request.cookies.get(COOKIE_NAME)
        if sid:
            sessions().pop(sid, None)
        response = see_other("/login")
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/work-orders")
    def work_orders(request: Request) -> Response:
        session = current_session(request)
        if session is None:
            return login_redirect()
        try:
            status_code, body = _forward_list(mock_base_url, session.token)
        except requests.RequestException as exc:
            return unreachable("读取工单列表", exc)
        if status_code in {401, 403}:
            # 令牌在 mock_server 侧失效（例如它重启了）：清掉本地会话，让用户重新登录，
            # 而不是把 401 渲染成一张空列表——那会装成「真的一单都没有」。
            sessions().pop(request.cookies.get(COOKIE_NAME) or "", None)
            return login_redirect()
        if status_code != 200:
            return upstream_error("读取工单列表", status_code, str(body))
        items = body.get("items") or []
        return HTMLResponse(render_work_orders_page([dict(item) for item in items], session.username))

    @app.post("/work-orders/{order_no}/approve")
    def approve(request: Request, order_no: str) -> Response:
        """审批：先查详情拿当前 `version`，再带上它转发审批（乐观锁，契约 §4）。

        为什么不把 version 塞进表单：放页面里会随渲染一起过期，而且从 HTTP 层看
        「页面里有没有隐藏字段」是实现细节；服务端自己读一遍详情，是唯一不依赖渲染时序的做法。
        """
        session = current_session(request)
        if session is None:
            return login_redirect()
        try:
            detail_status, detail = _forward_detail(mock_base_url, session.token, order_no)
        except requests.RequestException as exc:
            return unreachable("读取工单详情", exc)
        if detail_status != 200 or "version" not in detail:
            return upstream_error("读取工单详情", detail_status, str(detail))

        try:
            status_code, text = _forward_approve(mock_base_url, session.token, order_no, int(detail["version"]))
        except requests.RequestException as exc:
            return unreachable("提交审批", exc)
        if status_code != 200:
            return upstream_error("提交审批", status_code, text)
        return see_other("/work-orders")

    return app
