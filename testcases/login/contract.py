"""把被测系统（login_app）的启动契约写成可执行文档。

契约本身不需要真实的网络服务也能验证 —— 它是对"服务应该长什么样"的超前断言。
这样做的价值：**契约漂移会在最早的时点、以最清晰的方式失败**，而不是等到
某条业务用例报一个看不懂的 ConnectionError。

注意这里**不起服务**：真起服务要几十毫秒且可能因端口/环境失败，
而契约校验是纯静态的，放在这里跑几十毫秒就能给出确定的结论。
"""

from __future__ import annotations

CONTRACT: dict = {
    "title": "AgentTest Demo — 登录服务",
    "paths": {
        "/api/health": {"GET"},
        "/api/login": {"POST"},
        "/api/flaky": {"GET"},
    },
    # 登录接口的响应契约：状态码 -> 语义
    "login_status_semantics": {
        200: "登录成功，返回 token 与 user",
        400: "缺少用户名或密码 / 请求体不是合法 JSON",
        401: "密码错误",
    },
    # 契约里**没有**定义、但实现额外兼容的行为（FIND-02）
    "undocumented_behaviour": {
        "form_submission": "契约只定义 application/json，实现额外接受表单提交",
    },
}


def undeclared_status_codes() -> set[int]:
    """实现可能返回、但契约里没写的状态码。

    主要用于提醒：改动实现时新加的状态码要同步进契约，否则"契约测试"
    就退化成"实现测试"—— 实现返回什么就断言什么，失去了发现偏差的能力。
    """
    return {405, 422, 500}
