"""被测系统（SUT）：登录服务，故意保留 6 个"发现点"。

## 为什么这个服务被写得很"糙"

正常的被测系统应该是**正确的**，用例全绿才算通过。但登录服务这一套反过来：
它刻意保留了 6 个真实项目里常见的实现缺陷，用例的作用是**把这些缺陷钉住**
（断言"当前行为就是这样"），而不是断言"它是对的"。

这么做的价值在于：**"发现缺陷"是测试工程师的核心产出，但这件事很难在简历项目里
体现** —— 打公网练习站的用例全绿，说明不了你会找 bug，只说明你会写断言。
把缺陷固化成会跑的用例，等于把"我发现过这些问题"变成了可验证的证据。

## 6 个发现点（对应用例里的 LC 编号）

| 编号     | 现象                                        | 级别 | 为什么是问题 |
|----------|---------------------------------------------|------|--------------|
| FIND-01  | 任意用户名 + 密码 123456 都能登录（LC-02）  | 中   | 没有用户存在性校验，等于没有认证 |
| FIND-02  | 额外接受表单提交，契约只定义了 JSON（LC-05）| 低   | 契约偏差（宽松方向），客户端会依赖上未定义行为 |
| FIND-03  | username 做 trim、password 不做（LC-03/17） | 低   | 同一份输入两个字段处理不对称，容易出"明明输对了却登不上" |
| FIND-04  | 用户名/密码无长度限制（LC-18）              | 低   | 1000 字符用户名照样 200，缺输入边界防护 |
| FIND-05  | 用户名原样回显（LC-20）                     | 中   | 前端若未转义即为 XSS |
| FIND-06  | token 是固定值 demo-token-123（LC-27）      | 中   | 无签发时间、无过期、不可撤销，生产不可用 |

## 为什么从 Flask 改写成 FastAPI

原实现是 Flask。搬进本项目时改成了 FastAPI，有三个理由：

1. **本项目的 Mock 后端（mock_server/）已经是 FastAPI**，再引入 Flask 会让
   requirements.txt 同时背两套 Web 框架，而两套在这里没有任何一方不可替代；
2. 用例测的是**行为契约**（trim 不对称、固定 token、长度不限），不是框架 API，
   换框架后逐条行为都能原样保持 —— 下面 6 个发现点的行为一个都没变；
3. 面试时"我用 FastAPI 写过被测服务"比"我装了个 Flask"更贴近真实岗位的技术栈。

## 行为冻结的写法

下面每个发现点都用一个模块级常量开关，作用不是"可以关掉"，而是**让审阅的人一眼
看到这里是有意为之**。改这些常量会让对应用例变红，这正是我们要的：行为一旦被
谁"顺手修好了"，测试会立刻告诉你，而不是悄悄失去意义。
"""

from __future__ import annotations

import random
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_TITLE = "AgentTest Demo — 登录服务"
APP_VERSION = "1.0.0"

# 唯一的"正确密码"。FIND-01 的根源：只校验密码，不校验用户是否存在。
CORRECT_PASSWORD = "123456"

# ---------- 行为开关：默认全开，改任意一个都会让对应用例变红 ----------
FIND_01_NO_USER_CHECK = True      # 不校验用户是否存在（任意用户名 + 正确密码都能登）
FIND_02_ACCEPT_FORM = True        # 兼容表单提交（契约只定义了 application/json）
FIND_03_TRIM_ASYMmetry = True     # username 做 trim、password 不做
FIND_04_NO_LENGTH_LIMIT = True    # 不限制输入长度
FIND_05_ECHO_USERNAME = True      # 原样回显用户名
FIND_06_FIXED_TOKEN = True        # 固定 token

# 用于 FIND-06：固定 token 的字面量。用例直接断言它等于这个值。
FIXED_TOKEN = "demo-token-123"


def _looks_like_form(content_type: str) -> bool:
    """判断请求体的 content-type 是否可能是表单。

    只有 urlencoded 和 multipart 才该走 request.form()。其余（text/plain、
    空、application/json 之外的任何东西）一律不解析 —— 这是复刻原 Flask 实现
    `request.get_json(silent=True) or request.form.to_dict()` 里 form 的
    "不是表单就返回空"语义，避免 Starlette 直接抛异常导致 500。
    """
    return ("application/x-www-form-urlencoded" in content_type) or (
        "multipart/form-data" in content_type
    )


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, version=APP_VERSION)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": APP_VERSION}

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        """登录。返回 200 / 400 / 401 三种结果。

        读请求体的顺序刻意保留"先 JSON、退回表单"：
        - JSON 解析失败时 FastAPI 默认会直接抛 422，而契约要求 400。
          所以这里**手动解析**而不是用 pydantic 模型 —— 一旦交给 pydantic，
          "缺字段"会变成 422 而不是契约里的 400，用例会红在框架行为上，
          而不是红在我们要验证的业务逻辑上。
        - 非 JSON 请求体（如 text/plain）也必须落到 400，见 LC-29。
        """
        data: dict[str, Any] = {}

        content_type = (request.headers.get("content-type") or "").lower()

        if "application/json" in content_type:
            try:
                parsed = await request.json()
            except Exception:  # noqa: BLE001 - 非法 JSON 体一律按 400 处理
                return JSONResponse({"error": "invalid json body"}, status_code=400)
            if isinstance(parsed, dict):
                data = parsed
        elif FIND_02_ACCEPT_FORM and _looks_like_form(content_type):
            # FIND-02：契约只定义了 application/json，这里额外兼容表单。
            #
            # `_looks_like_form` 这个判断不能省：原 Flask 实现用
            # `request.get_json(silent=True) or request.form.to_dict()`，
            # form 解析在 content-type 不是表单时会优雅地返回空 dict。
            # 而 Starlette 的 request.form() 在 content-type 为 text/plain 时
            # 会**直接抛异常**，不判断就会让 LC-29（非 JSON 体）变成 500 而不是 400。
            #
            # 这里**不用 request.form()**，而是手工解析 urlencoded 请求体：
            # Starlette 的 form() 依赖 python-multipart，缺了它直接抛
            # AssertionError("The `python-multipart` library must be installed")。
            # 本项目只兼容 urlencoded 这一种表单（LC-05 测的就是它），
            # 用标准库 parse_qs 解析既能保住行为，又不必为一条用例
            # 往 requirements 里加一个运行期依赖 —— 被测服务的依赖越少，
            # "照 README 装完却跑不起来"的机会就越少。
            try:
                raw_body = (await request.body()).decode("utf-8")
                data = {k: v[0] for k, v in parse_qs(raw_body).items() if v}
            except Exception:  # noqa: BLE001
                data = {}

        raw_username = data.get("username")
        raw_password = data.get("password")

        # 先把"原实现里 `data.get(k) or ""` 的语义搬过来：**falsy 值一律当成没传**。
        # 这一步是 LC-13 的关键：JSON 里传 `"password": 0` 时，0 是 falsy，
        # 原实现会把它当成"缺字段"返回 400，而不是当成密码 "0" 去比对。
        # 用 FastAPI 重写时如果直接 str(raw_password)，0 会变成 "0" 这种非空字符串，
        # 一路走到密码校验返回 401 —— 那是对原行为的偏离，不是改进。
        username_value = raw_username or ""
        password_value = raw_password or ""

        if FIND_03_TRIM_ASYMmetry:
            # FIND-03：username 做 strip，password 不做。
            # 这一行就是要制造 LC-03（"  admin  " 能登）与 LC-17（"123456 "
            # 登不上）之间的不对称。
            username = str(username_value).strip()
            password = str(password_value)
        else:
            username = str(username_value)
            password = str(password_value)

        if not username or not password:
            return JSONResponse({"error": "username and password are required"}, status_code=400)

        if FIND_04_NO_LENGTH_LIMIT is False:
            if len(username) > 64 or len(password) > 64:
                return JSONResponse({"error": "input too long"}, status_code=400)

        if password != CORRECT_PASSWORD:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)

        # 走到这里 = 密码对了。FIND-01：完全不校验 username 是不是真实用户。
        if not FIND_01_NO_USER_CHECK:
            if username not in {"admin"}:
                return JSONResponse({"error": "user not found"}, status_code=404)

        token = FIXED_TOKEN if FIND_06_FIXED_TOKEN else f"tok-{random.random():.16f}"
        user = username if FIND_05_ECHO_USERNAME else "<hidden>"
        return JSONResponse({"token": token, "user": user}, status_code=200)

    @app.get("/api/flaky")
    def flaky() -> JSONResponse:
        """约 10% 概率 503：模拟不稳定服务，用来验证框架的重试与代理回退。

        原 demo_app 里就有这个端点，一并保留 —— 它是"重试策略真的有用"的
        现场证据，而不是 README 里的一句声明。
        """
        if random.random() < 0.1:
            return JSONResponse({"error": "service temporarily unavailable"}, status_code=503)
        return JSONResponse({"flaky": True, "ok": True})

    return app


app = create_app()
