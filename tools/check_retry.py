"""自检脚本：验证 BaseApi 的重试策略与代理回退（发假的请求，用桩对象）。

为什么写这个：重试和代理回退都是"只在出问题时才跑到"的代码，正常测试永远覆盖不到。
它们一旦写错（误重试 POST、代理没被绕过、该抛的异常被吞掉），
后果是假失败或者脏数据。这里用桩对象把这些路径都逼出来。
"""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from apis.base import BaseApi  # noqa: E402

logging.disable(logging.WARNING)  # 自检输出保持干净


def make_response(status_code: int, text: str = "{}") -> Mock:
    r = Mock(spec=requests.Response)
    r.status_code = status_code
    r.ok = status_code < 400
    r.text = text
    return r


def run_case(
    name: str,
    method: str,
    responses,
    expected_calls: int,
    expect_raise=None,
    retries: int = 2,
    inspect=None,
    kwargs=None,
) -> bool:
    """跑一条路径。inspect(api, response) 返回 (bool, str) 做额外校验。

    kwargs: 传给 request() 的额外参数（测试 params 等透传场景用）。
    """
    api = BaseApi("https://example.com", timeout=5, retries=retries)
    api.session = Mock()
    api.session.request = Mock(side_effect=responses)
    response = None
    try:
        response = api.request(method, "/x", **(kwargs or {}))
    except Exception as exc:  # noqa: BLE001
        if expect_raise and isinstance(exc, expect_raise):
            print(f"  [OK] {name}: 按预期抛出 {type(exc).__name__}")
            return True
        print(f"  [FAIL] {name}: 意外异常 {type(exc).__name__}: {exc}")
        return False

    calls = api.session.request.call_count
    ok = calls == expected_calls
    detail = f"实际请求 {calls} 次" if ok else f"期望 {expected_calls} 次，实际 {calls} 次"
    if ok and inspect:
        ok, detail = inspect(api, response)
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def main() -> int:
    print("===== BaseApi 重试策略自检 =====")
    results = []

    # 1. GET 遇到 500：应当重试到用尽（1 次原始 + 2 次重试 = 3 次）
    results.append(
        run_case(
            "GET 500 重试到用尽",
            "GET",
            [make_response(500), make_response(500), make_response(500)],
            expected_calls=3,
        )
    )

    # 2. POST 遇到 500：绝对不能重试（重放会造重复数据）
    results.append(
        run_case(
            "POST 500 不重试",
            "POST",
            [make_response(500), make_response(500), make_response(500)],
            expected_calls=1,
        )
    )

    # 3. POST 遇到 429：请求没被处理，重试是安全的
    results.append(
        run_case(
            "POST 429 重试",
            "POST",
            [make_response(429), make_response(201)],
            expected_calls=2,
        )
    )

    # 4. GET 第二次就成功：不该多打第三次
    results.append(
        run_case(
            "GET 500 后成功即停",
            "GET",
            [make_response(500), make_response(200)],
            expected_calls=2,
        )
    )

    # 5. 连接失败：重试到用尽后把异常抛出去（不能吞掉异常假装通过）
    results.append(
        run_case(
            "连接失败重试后抛出",
            "GET",
            requests.ConnectionError("boom"),
            expected_calls=3,
            expect_raise=requests.ConnectionError,
        )
    )

    # 6. 2xx 正常路径：只请求一次
    results.append(
        run_case(
            "200 只请求一次",
            "GET",
            [make_response(200)],
            expected_calls=1,
        )
    )

    # 7. 代理掐断 TLS 后，重试必须绕过代理（本机开着 Clash 时的真实场景）
    def proxy_bypassed(api, response):
        calls = api.session.request.call_args_list
        first = calls[0].kwargs.get("proxies")
        second = calls[1].kwargs.get("proxies")
        if first is not None:
            return False, f"第一次就绕过了代理({first})，不该如此"
        if second != {"http": None, "https": None}:
            return False, f"重试没有绕过代理(proxies={second})"
        return True, "首次走代理、重试绕过代理"

    os.environ.pop("API_TRUST_ENV", None)
    results.append(
        run_case(
            "代理失败后重试绕过代理",
            "GET",
            [requests.exceptions.SSLError("SSLEOFError: UNEXPECTED_EOF_WHILE_READING"), make_response(200)],
            expected_calls=2,
            retries=1,
            inspect=proxy_bypassed,
        )
    )

    # 8. 显式关掉回退（API_TRUST_ENV=0）时，必须老老实实报错，不能偷偷绕过
    os.environ["API_TRUST_ENV"] = "0"
    results.append(
        run_case(
            "API_TRUST_ENV=0 时不绕过代理",
            "GET",
            [requests.exceptions.SSLError("boom"), make_response(200)],
            expected_calls=2,
            retries=1,
            inspect=lambda api, response: (
                (api.session.request.call_args_list[1].kwargs.get("proxies") is None),
                "重试仍走代理（符合 API_TRUST_ENV=0）",
            ),
        )
    )
    os.environ.pop("API_TRUST_ENV", None)

    # ---------------------------------------------------------------
    # 以下三条是补充验证：代码逐层解读.md 里声称发现了几个 bug（W1/W2/W3），
    # 这些说法必须用实测判定真伪，不能靠读代码下结论。
    # ---------------------------------------------------------------

    # 9. PATCH 遇到 500：不该重试（RFC 意义上 PATCH 不幂等）。
    #    这条同时用来核对「PATCH 实测只请求 1 次」这个说法是否属实。
    results.append(
        run_case(
            "PATCH 500 不重试",
            "PATCH",
            [make_response(500), make_response(500), make_response(500)],
            expected_calls=1,
        )
    )

    # 10. PUT 遇到 500：幂等，应该重试到用尽（与 PATCH 形成对照）
    results.append(
        run_case(
            "PUT 500 重试到用尽",
            "PUT",
            [make_response(500), make_response(500), make_response(500)],
            expected_calls=3,
        )
    )

    # 11. was_retried 标记：重试过的响应必须是 True
    results.append(
        run_case(
            "重试过的响应 was_retried=True",
            "GET",
            [make_response(500), make_response(200)],
            expected_calls=2,
            inspect=lambda api, response: (
                getattr(response, "was_retried", None) is True,
                f"was_retried={getattr(response, 'was_retried', '未设置')}",
            ),
        )
    )

    # 12. was_retried 标记：没重试的响应必须是 False（不能恒为 True）
    results.append(
        run_case(
            "没重试的响应 was_retried=False",
            "GET",
            [make_response(200)],
            expected_calls=1,
            inspect=lambda api, response: (
                getattr(response, "was_retried", None) is False,
                f"was_retried={getattr(response, 'was_retried', '未设置')}",
            ),
        )
    )

    passed = sum(results)
    print(f"\n结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
