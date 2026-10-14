"""MES Mock 后端的接口封装层。

设计要点（和 user_api.py 保持一致：用例里不出现裸 requests）：
1. 继承 BaseApi，复用超时/重试/日志/Allure 附加那一套；
2. **多角色 token 缓存**：同一个 MesApi 实例可以同时代表多个角色
   （`as_role("ADMIN")` 返回一个带该角色 token 的视图）。
   为什么需要：真实项目里"用 A 角色造数、用 B 角色验证权限"是常态，
   如果每个角色都新建一个 api 对象，token 的刷新与传递很快就会失控。
3. 错误断言收口到 `expect_error()`：业务代码（code）才是契约，
   HTTP 状态码只是载体 —— 只断言 400 会漏掉"该返回 409 却返回 400"这类
   前后端约定漂移。用例统一写 `mes.expect_error(resp, 403, "PERMISSION_DENIED")`。
"""

from __future__ import annotations

import json
from typing import Any

from apis.base import BaseApi
from apis.token_provider import LoginResult, TokenProvider
from common.secrets import ROLE_ACCOUNTS, role_password, role_username

__all__ = ["ROLE_ACCOUNTS", "ApiError", "MesApi", "RoleClient"]

# 幂等键请求头。真实网关（Kong/APISIX/Nginx 插件）也是这个头名。
IDEMPOTENCY_HEADER = "Idempotency-Key"
# 领料单关联工单号：Mock 服务从请求头取，模拟"网关把上下文透传下来"
WORK_ORDER_HEADER = "X-Work-Order-No"


class ApiError(Exception):
    """业务错误断言失败时抛出，信息里带上 code/message/detail 便于定位。"""


class RoleClient:
    """以某个角色发请求的轻量视图，共享底层 requests.Session。

    共享 Session 的好处是连接复用、日志统一；token 通过显式 header 传，
    不污染 Session 的默认 Authorization，避免"用例 A 设了 ADMIN token，
    用例 B 以为自己是 OPERATOR"这种串号事故。

    token 由 `TokenProvider` 持有，**不在这里缓存成一个字符串**。
    改造前这里存的是 `self.token`（登录那一刻的字符串），后果是：
    令牌在别处刷新之后，这个视图仍然拿着旧值继续发请求 —— 也就是
    "同一个对象里两个令牌来源"这种串号/过期事故的典型形态。
    现在 `token` 变成只读属性、每次都向 provider 要当前值，
    `_headers()` 每发一次请求就取一次，刷新对已缓存的视图立刻可见。
    """

    def __init__(self, api: "MesApi", provider: TokenProvider, username: str, role: str):
        self._api = api
        self._provider = provider
        self.username = username
        self.role = role

    @property
    def token(self) -> str:
        """当前令牌（读时向 provider 取，可能触发一次提前刷新）。"""
        return self._provider.token

    @property
    def provider(self) -> TokenProvider:
        return self._provider

    # ---------- 请求 ----------
    def _request(self, method: str, path: str, **kwargs):
        """把「令牌来源」随请求一起交给 BaseApi，由它在发包那一刻取当前令牌。

        为什么不再自己拼 `Authorization`：
        1. 自己拼的时候，头部在**进入重试循环之前**就固化成字符串了，
           而 401 重放的意义正是"用换过的新令牌再发一次"——令牌到不了那一层，
           重放只会再吃一个 401；
        2. 调用方显式传的 headers 仍然优先（`_with_token` 只在缺少该头时填），
           所以用例里手工构造 Authorization 的写法不受影响。
        """
        kwargs["_token_source"] = self._provider
        return getattr(self._api, method)(path, **kwargs)

    def get(self, path: str, **kwargs):
        return self._request("get", path, **kwargs)

    def post(self, path: str, json: Any = None, **kwargs):
        return self._request("post", path, json=json, **kwargs)

    def put(self, path: str, json: Any = None, **kwargs):
        return self._request("put", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs):
        return self._request("delete", path, **kwargs)

    def post_idem(self, path: str, key: str, json: Any = None, **kwargs):
        """带幂等键的 POST。幂等是这批用例的重点，单独给个入口提醒别忘传。"""
        headers = dict(kwargs.pop("headers", None) or {})
        headers[IDEMPOTENCY_HEADER] = key
        return self.post(path, json=json, headers=headers, **kwargs)


class MesApi(BaseApi):
    """MES Mock 后端客户端。路径前缀 /api/v1 由 mock_server.app 决定。"""

    def __init__(self, base_url: str, timeout: int = None, retries: int = None):
        # 默认不重试：Mock 服务返回的 4xx/409 是**业务结论**，不是网络抖动。
        # BaseApi 默认 retries=2 对 GET 的 5xx 会重试，这在被测系统上没问题，
        # 但我们的用例要精确数请求次数（幂等/并发），重试会让计数失真。
        super().__init__(base_url, timeout=timeout, retries=0 if retries is None else retries)
        self._roles: dict[str, RoleClient] = {}

    # ---------- 认证 ----------
    def login(self, username: str, password: str):
        """原始登录请求，供登录用例断言各种失败分支。"""
        return self.post("/auth/login", json={"username": username, "password": password})

    def login_ok(self, username: str, password: str) -> LoginResult:
        """登录并返回 LoginResult；登录失败直接抛，避免用例里到处 if。

        为什么返回值里带上了 `expires_in`（改造前只回 `(token, user)`）：
        令牌的到期时刻只能由服务端给的 ttl 推出，丢了这个字段，
        调用方就只能自己猜一个 TTL —— 两端不一致时客户端会以为还有效，
        于是"偶发 401"又回来了。这里是它唯一的来源，不能丢。
        """
        resp = self.login(username, password)
        if resp.status_code != 200:
            raise ApiError(f"登录失败：{username} -> {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        return LoginResult(token=body["access_token"], expires_in=int(body["expires_in"]), user=body["user"])

    def _provider_for(self, role: str) -> TokenProvider:
        """给角色造一个令牌来源；`fetch` 闭包每次真去登录一次。

        provider 负责"什么时候该换令牌"，`_fetch` 只负责"怎么登录"，
        两者分开之后，并发去重、提前刷新这些逻辑就能被单测直接驱动，
        不需要真的起一个会过期的服务。
        """
        username = role_username(role)
        password = role_password(role)
        return TokenProvider(lambda: self.login_ok(username, password), name=role)

    def as_role(self, role: str) -> RoleClient:
        """按角色取（并缓存）一个带 token 的客户端视图。

        第一次取视图时仍然立刻登录一次：登录失败（账号被停用、口令配错）
        必须在 `as_role()` 这一行就暴露出来，而不是推迟到某条业务请求上
        变成一句莫名其妙的 401。
        """
        if role not in self._roles:
            provider = self._provider_for(role)
            provider.refresh()
            user = provider.user
            assert user is not None  # refresh() 成功后必然有值
            self._roles[role] = RoleClient(self, provider, user["username"], user["role"])
        return self._roles[role]

    # ---------- 断言辅助 ----------
    @staticmethod
    def error_code(resp) -> str | None:
        try:
            return resp.json().get("code")
        except Exception:  # noqa: BLE001 - 非 JSON 响应（如 500 HTML）返回 None
            return None

    @classmethod
    def expect_error(cls, resp, status: int, code: str | None = None) -> str:
        """断言"失败响应的状态码 + 业务 code"，返回 code 便于继续断言 detail。

        为什么必须断言 code：Mock/真实后端都可能用 400 承载十几种不同原因，
        只断言 400 的话，"库存不足"和"状态非法"在报告里长得一模一样，
        出问题时无法区分是哪个校验失效了。
        """
        detail = resp.text[:500]
        if resp.status_code != status:
            raise ApiError(f"期望 HTTP {status}，实际 {resp.status_code}；响应: {detail}")
        actual = cls.error_code(resp)
        if code is not None and actual != code:
            raise ApiError(f"期望业务码 {code}，实际 {actual}；响应: {detail}")
        return actual or ""

    @classmethod
    def expect_ok(cls, resp, status: int = 200):
        if resp.status_code != status:
            raise ApiError(f"期望 HTTP {status}，实际 {resp.status_code}；响应: {resp.text[:500]}")
        return resp.json()

    @classmethod
    def expect_field(cls, body: dict, field: str, expected) -> None:
        """断言响应体里某个字段的值，失败信息带字段名和完整 JSON。

        直接写 `assert body["status"] == "PENDING"` 的缺点是：
        pytest 只会告诉你 "assert 'SHIPPED' == 'PENDING'"，不说是**哪个字段**错了，
        工单有十几个状态字段时，看报告的人要在脑子里反推半天。
        """
        if field not in body:
            raise ApiError(f"响应里没有字段 {field}；实际字段: {sorted(body)}")
        actual = body[field]
        if actual != expected:
            raise ApiError(
                f"字段 {field} 期望 {expected!r}，实际 {actual!r}；"
                f"完整响应: {json.dumps(body, ensure_ascii=False, default=str)[:500]}"
            )

    @classmethod
    def expect_field_in(cls, body: dict, field: str, allowed) -> None:
        """断言字段值在允许集合内（状态机这类"多合法取值"的场景用）。"""
        actual = body.get(field)
        if actual not in allowed:
            raise ApiError(
                f"字段 {field} 期望属于 {list(allowed)}，实际 {actual!r}；"
                f"完整响应: {json.dumps(body, ensure_ascii=False, default=str)[:500]}"
            )

    # ---------- 工单 ----------
    def work_order(self, order_no: str, role: str = "PLANNER"):
        """按角色读工单。

        必须带角色：所有业务端点都要 Bearer 令牌，用裸 self.get() 会 401。
        """
        return self.as_role(role).get(f"/work-orders/{order_no}")

    def work_order_version(self, order_no: str, role: str = "PLANNER") -> int:
        """取工单当前 version。

        乐观锁用例**绝对不能硬编码 version**：写死数字的用例会在别人插了一条
        case 之后集体变红，然后大家就去改成新数字——这样乐观锁其实从没被验证过。
        """
        resp = self.as_role(role).get(f"/work-orders/{order_no}")
        self.expect_ok(resp)
        return resp.json()["version"]

    def create_work_order(self, role: str, idem_key: str | None = None, **payload):
        """建工单。带 `idem_key` 时走幂等路径（重放/冲突用例必须用它）。

        "重试"这件事只有在**客户端把同一个键再发一次**时才成立；
        如果第一次没带键、第二次带键，服务端看到的是两个毫无关系的请求，
        重试就成了"又建一张单"。所以入口必须显式暴露这个参数。
        """
        if idem_key:
            return self.as_role(role).post_idem("/work-orders", idem_key, json=payload)
        return self.as_role(role).post("/work-orders", json=payload)

    # ---------- 库存 ----------
    def stock_of(self, material_code: str, warehouse: str = "WH-01", role: str = "ADMIN"):
        resp = self.as_role(role).get(
            "/inventory/stock", params={"material_code": material_code, "warehouse": warehouse}
        )
        self.expect_ok(resp)
        items = resp.json()["items"]
        if not items:
            raise ApiError(f"仓库 {warehouse} 没有物料 {material_code} 的库存记录")
        return items[0]

    def issue(
        self,
        role: str,
        *,
        material_code: str,
        qty: str,
        warehouse: str = "WH-01",
        idem_key: str | None = None,
        work_order_no: str | None = None,
        **extra,
    ):
        headers = {}
        if idem_key:
            headers[IDEMPOTENCY_HEADER] = idem_key
        if work_order_no:
            headers[WORK_ORDER_HEADER] = work_order_no
        payload = {"material_code": material_code, "qty": qty, "warehouse": warehouse}
        payload.update(extra)
        return self.as_role(role).post("/inventory/issue", json=payload, headers=headers)

    # ---------- 追踪 ----------
    def traceability(self, order_no: str, role: str = "QA"):
        return self.as_role(role).get(f"/work-orders/{order_no}/traceability")
