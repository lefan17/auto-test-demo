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

# 角色 -> 种子账号（与 mock_server/db.py 的 SEED_USERS 一致）
ROLE_ACCOUNTS: dict[str, tuple[str, str]] = {
    "ADMIN": ("admin", "admin123"),
    "PLANNER": ("planner01", "plan123"),
    "OPERATOR": ("operator01", "op123"),
    "OPERATOR2": ("operator02", "op123"),
    "QA": ("qa01", "qa123"),
}

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
    """

    def __init__(self, api: "MesApi", token: str, username: str, role: str):
        self._api = api
        self.token = token
        self.username = username
        self.role = role

    # ---------- 请求 ----------
    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra:
            headers.update(extra)
        return headers

    def get(self, path: str, **kwargs):
        return self._api.get(path, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def post(self, path: str, json: Any = None, **kwargs):
        return self._api.post(path, json=json, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def put(self, path: str, json: Any = None, **kwargs):
        return self._api.put(path, json=json, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def delete(self, path: str, **kwargs):
        return self._api.delete(path, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def post_idem(self, path: str, key: str, json: Any = None, **kwargs):
        """带幂等键的 POST。幂等是这批用例的重点，单独给个入口提醒别忘传。"""
        return self.post(path, json=json, headers={IDEMPOTENCY_HEADER: key}, **kwargs)


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

    def login_ok(self, username: str, password: str):
        """登录并返回 (token, user_dict)；登录失败直接抛，避免用例里到处 if。"""
        resp = self.login(username, password)
        if resp.status_code != 200:
            raise ApiError(f"登录失败：{username} -> {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        return body["access_token"], body["user"]

    def as_role(self, role: str) -> RoleClient:
        """按角色取（并缓存）一个带 token 的客户端视图。"""
        if role not in self._roles:
            username, password = ROLE_ACCOUNTS[role]
            token, user = self.login_ok(username, password)
            self._roles[role] = RoleClient(self, token, user["username"], user["role"])
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
