"""自检脚本：验证 BaseApi 的重试策略（不发真实请求，用假的 Session）。

为什么写这个：重试逻辑是"只在出问题时才跑到"的代码，正常测试永远覆盖不到。
它一旦写错（比如误重试 POST、比如该重试的没重试），后果是线上假失败或脏数据。
这里用桩对象把三条关键路径都逼出来。
"""
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from api.base import BaseApi  # noqa: E402


def make_response(status_code: int, text: str = "{}") -> Mock:
    r = Mock(spec=requests.Response)
    r.status_code = status_code
    r.ok = status_code < 400
    r.text = text
    return r


def run_case(name: str, method: str, responses, expected_calls: int, expect_raise=None):
    api = BaseApi("https://example.com", timeout=5, retries=2)
    api.session = Mock()
    api.session.request = Mock(side_effect=responses)
    try:
        api.request(method, "/x")
    except Exception as exc:  # noqa: BLE001
        if expect_raise and isinstance(exc, expect_raise):
            print(f"  [OK] {name}: 按预期抛出 {type(exc).__name__}")
            return True
        print(f"  [FAIL] {name}: 意外异常 {type(exc).__name__}: {exc}")
        return False

    calls = api.session.request.call_count
    if calls == expected_calls:
        print(f"  [OK] {name}: 实际请求 {calls} 次")
        return True
    print(f"  [FAIL] {name}: 期望 {expected_calls} 次，实际 {calls} 次")
    return False


def main() -> int:
    print("===== BaseApi 重试策略自检 =====")
    results = []

    # 1. GET 遇到 500：应当重试到用尽（1 次原始 + 2 次重试 = 3 次）
    results.append(run_case(
        "GET 500 重试到用尽", "GET",
        [make_response(500), make_response(500), make_response(500)],
        expected_calls=3,
    ))

    # 2. POST 遇到 500：绝对不能重试（重放会造重复数据）
    results.append(run_case(
        "POST 500 不重试", "POST",
        [make_response(500), make_response(500), make_response(500)],
        expected_calls=1,
    ))

    # 3. POST 遇到 429：请求没被处理，重试是安全的
    results.append(run_case(
        "POST 429 重试", "POST",
        [make_response(429), make_response(201)],
        expected_calls=2,
    ))

    # 4. GET 第二次就成功：不该多打第三次
    results.append(run_case(
        "GET 500 后成功即停", "GET",
        [make_response(500), make_response(200)],
        expected_calls=2,
    ))

    # 5. 连接失败：重试到用尽后把异常抛出去（不能吞掉异常假装通过）
    results.append(run_case(
        "连接失败重试后抛出", "GET",
        requests.ConnectionError("boom"),
        expected_calls=3,
        expect_raise=requests.ConnectionError,
    ))

    # 6. 2xx 正常路径：只请求一次
    results.append(run_case(
        "200 只请求一次", "GET",
        [make_response(200)],
        expected_calls=1,
    ))

    passed = sum(results)
    print(f"\n结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
