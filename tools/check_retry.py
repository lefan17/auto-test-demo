"""自检脚本：验证 BaseApi 的重试策略与代理回退（发假的请求，用桩对象）。

为什么写这个：重试和代理回退都是"只在出问题时才跑到"的代码，正常测试永远覆盖不到。
它们一旦写错（误重试 POST、代理没被绕过、该抛的异常被吞掉），
后果是假失败或者脏数据。这里用桩对象把这些路径都逼出来。
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from apis.base import BaseApi  # noqa: E402

logging.disable(logging.WARNING)  # 自检输出保持干净


def make_response(status_code: int, text: str = "{}", headers=None) -> Mock:
    r = Mock(spec=requests.Response)
    r.status_code = status_code
    r.ok = status_code < 400
    r.text = text
    # 显式给 {} 而不是让 Mock 自动生成：`Mock().headers` 是个 Mock，
    # 任何 `headers.get(...)` 都会返回 Mock 而不是 None，
    # 「没有 Retry-After 头」这条最常见的路径反而测不到。
    r.headers = dict(headers or {})
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

    # ---------------------------------------------------------------
    # 波 2：等待策略（尊重 Retry-After + 有界退避）。
    # 下面的用例一条都不能真的等下去，所以把 api._sleep 换成记录器：
    # 断言"等了多久"靠的是记下来的数字，不是墙上的钟。
    # ---------------------------------------------------------------

    def capture_waits(responses, method="GET", retries=2, **api_kwargs):
        """跑一条请求路径，返回 (api, 每次重试前记录的等待秒数列表)。"""
        api = BaseApi("https://example.com", timeout=5, retries=retries, **api_kwargs)
        api.session = Mock()
        api.session.request = Mock(side_effect=responses)
        waits = []
        api._sleep = waits.append
        try:
            api.request(method, "/x")
        except Exception:  # noqa: BLE001 - 这里只关心等待序列，异常类型由别的用例管
            pass
        return api, waits

    def approx(actual: float, expected: float, tol: float = 0.2) -> bool:
        return abs(actual - expected) <= tol

    # 13. `Retry-After: 2` 必须赢过退避值：退避基数这次是 0.5s（含抖动也不到 0.625s），
    #     所以"等约 2s"这个断言只在真的读了头的时候才成立。
    api13, waits13 = capture_waits([make_response(429, headers={"Retry-After": "2"}), make_response(200)])
    ok13 = len(waits13) == 1 and approx(waits13[0], 2.0)
    print(f"  [{'OK' if ok13 else 'FAIL'}] Retry-After: 2 按头值等待: waits={waits13}")
    results.append(ok13)

    # 14. HTTP-date（未来 20 秒）→ 解析出约 20s。日期没带 GMT 后缀也一样要认。
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    api14, waits14 = capture_waits([make_response(429, headers={"Retry-After": future}), make_response(200)], retries=1)
    ok14 = len(waits14) == 1 and approx(waits14[0], 20.0, tol=2.0)
    print(f"  [{'OK' if ok14 else 'FAIL'}] Retry-After 为 HTTP-date({future}) 解析出约 20s: waits={waits14}")
    results.append(ok14)

    # 15. 已经过去的 HTTP-date → 立刻重试（等待 0），不是退回退避、更不是负数睡眠。
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=30), usegmt=True)
    _, waits15 = capture_waits([make_response(429, headers={"Retry-After": past}), make_response(200)], retries=1)
    ok15 = len(waits15) == 1 and waits15[0] == 0
    print(f"  [{'OK' if ok15 else 'FAIL'}] Retry-After 为过去时刻 → 不等待: waits={waits15}")
    results.append(ok15)

    # 16/17. 非法值（`abc` 读不懂、`-1` 不合规）→ 退回指数退避，且不能抛异常。
    #        退避下界 0.5s、上界 0.5*(1+0.25)=0.625s，正好把"退回退避"和"照头等待"区分开。
    for bad in ("abc", "-1"):
        _, waits = capture_waits([make_response(429, headers={"Retry-After": bad}), make_response(200)], retries=1)
        ok = len(waits) == 1 and 0.5 <= waits[0] <= 0.625
        print(f"  [{'OK' if ok else 'FAIL'}] Retry-After 非法值({bad}) 退回退避: waits={waits}")
        results.append(ok)

    # 18. 超大值必须被 cap 住：`Retry-After: 3600` 绝不能让用例真的等一小时。
    _, waits18 = capture_waits(
        [make_response(429, headers={"Retry-After": "3600"}), make_response(200)], retries=1, max_retry_wait=5.0
    )
    ok18 = len(waits18) == 1 and waits18[0] == 5.0
    print(f"  [{'OK' if ok18 else 'FAIL'}] Retry-After: 3600 被 cap 到上限: waits={waits18}")
    results.append(ok18)

    # 19. 回归：没有 Retry-After 头时，等待必须仍落在改造前的退避区间内
    #     （第 1 次重试是 0.5s，只多不少，绝不因为这次改造变成"等更短"）。
    _, waits19 = capture_waits([make_response(500), make_response(200)], retries=1)
    ok19 = len(waits19) == 1 and 0.5 <= waits19[0] <= 0.625
    print(f"  [{'OK' if ok19 else 'FAIL'}] 无 Retry-After 头时退回退避(回归): waits={waits19}")
    results.append(ok19)

    # 20. 传输失败的等待：没有响应可看，只能走退避，且不该因为新逻辑而变短。
    _, waits20 = capture_waits(requests.ConnectionError("boom"), retries=1)
    ok20 = len(waits20) == 1 and 0.5 <= waits20[0] <= 0.625
    print(f"  [{'OK' if ok20 else 'FAIL'}] 传输失败走退避: waits={waits20}")
    results.append(ok20)

    # 21. 抖动有界：多次取值必须落在闭区间 [base, base*(1+ratio)] 里，
    #     而不是"看起来大概是随机的"。
    jitter_api = BaseApi("https://example.com", timeout=5, retries=2)
    samples = [jitter_api._retry_wait(0) for _ in range(200)]
    ok21 = all(0.5 <= s <= 0.625 for s in samples) and len(set(samples)) > 1
    print(
        f"  [{'OK' if ok21 else 'FAIL'}] 抖动有界且非定值: min={min(samples):.4f} max={max(samples):.4f} 取值数={len(set(samples))}"
    )
    results.append(ok21)

    # 22. 抖动需要由实例自己的 RNG 产生：两个实例各自独立初始化，
    #     不该因为共享全局 random 状态而互相影响（并发用例下这点很关键）。
    a, b = BaseApi("https://example.com", timeout=5, retries=2), BaseApi("https://example.com", timeout=5, retries=2)
    ok22 = a._rng is not b._rng
    print(f"  [{'OK' if ok22 else 'FAIL'}] 每个实例持有独立 RNG: {a._rng is not b._rng}")
    results.append(ok22)

    # ---------------------------------------------------------------
    # 23. 代理回退的判据：只有「上一次尝试就是传输失败」，才绕过代理。
    #     这是 CHANGELOG「尚未修复的已知问题」里那条缺陷的钉子（当前故意红）。
    # ---------------------------------------------------------------
    user_proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    bypass = {"http": None, "https": None}

    def proxies_of(api, idx):
        """这条路径上没有真实网络，所以「有没有绕过代理」只能看每次调用带的 proxies。"""
        return api.session.request.call_args_list[idx].kwargs.get("proxies")

    # 序列：传输失败 → 500 → 200。第 2 次绕过代理是回退功能本身（必须成立），
    # 但第 3 次是「由状态码触发的重试」，此时上一次尝试拿到了响应、根本没碰代理，
    # 所以用户显式配的代理必须被还原，不能继续绕过。
    # 现实现把 `last_error is not None` 当判据，而它在拿到响应后从不复位 ——
    # 它记的是「本次请求曾经出过传输失败」，于是第 3 次也绕过 —— 本条因此红。
    prev_trust_env = os.environ.get("API_TRUST_ENV")
    os.environ["API_TRUST_ENV"] = "1"
    try:
        results.append(
            run_case(
                "传输失败后的状态码重试不绕过代理",
                "GET",
                [requests.ConnectionError("boom"), make_response(500), make_response(200)],
                expected_calls=3,
                kwargs={"proxies": user_proxies},
                inspect=lambda api, response: (
                    proxies_of(api, 0) == user_proxies
                    and proxies_of(api, 1) == bypass
                    and proxies_of(api, 2) == user_proxies,
                    f"三次请求带的 proxies: {[proxies_of(api, i) for i in range(3)]}",
                ),
            )
        )
    finally:
        if prev_trust_env is None:
            os.environ.pop("API_TRUST_ENV", None)
        else:
            os.environ["API_TRUST_ENV"] = prev_trust_env

    passed = sum(results)
    print(f"\n结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
