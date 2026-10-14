"""端到端验证：`BaseApi` 真的按 `Retry-After` 头等待，而不是只看内部常量。

为什么单独起一个本地 HTTP 服务，而不是继续用 `tools/check_retry.py` 里的 Mock：
那边证明的是"分支选对了"，它证明不了"这个头是从真实响应里读出来的"。
`requests` 的 `CaseInsensitiveDict`、大小写、header 折行、HTTP 版本差异，
全都只在真的过一遍 socket 时才可能出现。用 Mock 冒充端到端，等于把
"头没读到" 这类最要命的 bug 留在测试的盲区里。

为什么每条用例都把 `api._sleep` 换掉：真实等待会让这个文件从毫秒级变成
十几秒级，而 CI 里没人会为了一条回归用例多等 20 秒；等待时长本身是可以用
记录器断言的，不需要真的花掉这段时间。为了不把"没真的等"这件事变成空话，
每条用例都额外断言墙上时钟（证明等待确实没发生）。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apis.base import BaseApi

pytestmark = pytest.mark.api

BODY = b'{"ok": true}'


@contextmanager
def scripted_server(script):
    """起一个按脚本依次回响应的真实 HTTP 服务。

    script 里的每一项是 `(status_code, headers)`；请求超出脚本长度时，
    一直重复最后一项（用来造"重试到用尽"的场景）。
    产出 `(base_url, state)`，`state["hits"]` 是服务端真实收到的请求数——
    这个数字来自 socket 的另一端，不是 Mock 的 call_count。
    """
    state = {"hits": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            with lock:
                index = state["hits"]
                state["hits"] += 1
            status, headers = script[min(index, len(script) - 1)]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle

        def log_message(self, *args):  # 压掉默认的 stderr 噪音
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_api(base_url, retries=2, **kwargs):
    """建一个等待被记录的 BaseApi；返回 (api, waits)。"""
    api = BaseApi(base_url, timeout=5, retries=retries, **kwargs)
    waits: list[float] = []
    api._sleep = waits.append
    return api, waits


def test_delta_seconds_wins_over_exponential_backoff():
    """`Retry-After: 2` 必须真的等 2 秒，而不是内部退避的 0.5s。"""
    with scripted_server([(429, {"Retry-After": "2"}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url)
        start = time.perf_counter()
        response = api.request("GET", "/x")
        wall = time.perf_counter() - start

    assert state["hits"] == 2, "429 之后必须真的重试了一次"
    assert waits == [pytest.approx(2.0)], f"应等 2s，实际 {waits}"
    assert response.status_code == 200
    assert api.last_response is response
    assert response.was_retried is True
    assert wall < 1.0, f"等待必须是可注入的，不该真的花掉 2 秒（实际 {wall:.3f}s）"


def test_http_date_format_is_parsed():
    """HTTP-date 格式（RFC 9110 允许的另一种写法）也要解析成秒数。"""
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=15), usegmt=True)
    with scripted_server([(429, {"Retry-After": when}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url, retries=1)
        start = time.perf_counter()
        response = api.request("GET", "/x")
        wall = time.perf_counter() - start

    assert state["hits"] == 2
    assert waits == [pytest.approx(15.0, abs=2.0)], f"应等约 15s，实际 {waits}"
    assert response.status_code == 200
    assert wall < 1.0


def test_past_http_date_retries_immediately():
    """已经过去的日期说明"现在就能重试"：等待 0，而不是退回退避或睡负数。"""
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=60), usegmt=True)
    with scripted_server([(429, {"Retry-After": past}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url, retries=1)
        response = api.request("GET", "/x")

    assert state["hits"] == 2
    assert waits == [0], f"过去的日期应立刻重试，实际 {waits}"
    assert response.status_code == 200


def test_huge_retry_after_is_capped():
    """`Retry-After: 3600` 必须被上限截断，否则一条用例会挂一小时。"""
    with scripted_server([(429, {"Retry-After": "3600"}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url, retries=1, max_retry_wait=3.0)
        start = time.perf_counter()
        response = api.request("GET", "/x")
        wall = time.perf_counter() - start

    assert state["hits"] == 2
    assert waits == [3.0], f"应被 cap 到 3.0，实际 {waits}"
    assert response.status_code == 200
    assert wall < 1.0


def test_invalid_retry_after_falls_back_to_backoff():
    """非法值退回指数退避，而不是抛异常把整条请求炸掉。"""
    with scripted_server([(429, {"Retry-After": "not-a-number"}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url, retries=1)
        response = api.request("GET", "/x")

    assert state["hits"] == 2
    assert len(waits) == 1
    assert 0.5 <= waits[0] <= 0.625, f"应落在退避区间 [0.5, 0.625]，实际 {waits}"
    assert response.status_code == 200


def test_missing_header_keeps_backoff_regression():
    """没有 `Retry-After` 头时的行为必须与改造前一致（回归）。"""
    with scripted_server([(429, {}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url, retries=1)
        response = api.request("GET", "/x")

    assert state["hits"] == 2
    assert len(waits) == 1
    assert 0.5 <= waits[0] <= 0.625, f"改造不该让无头时的等待变短或变长，实际 {waits}"
    assert response.status_code == 200


def test_post_429_respects_retry_after():
    """POST 只在 429 重试这条语义不变，且同样吃 `Retry-After`。"""
    with scripted_server([(429, {"Retry-After": "1"}), (201, {})]) as (base_url, state):
        api, waits = make_api(base_url)
        response = api.request("POST", "/x", json={"a": 1})

    assert state["hits"] == 2
    assert waits == [pytest.approx(1.0)]
    assert response.status_code == 201
    assert response.was_retried is True


def test_gives_up_and_returns_last_response():
    """重试用尽后返回最后一个响应（429），而不是抛异常或返回中间那次。"""
    with scripted_server([(429, {"Retry-After": "1"})]) as (base_url, state):
        api, waits = make_api(base_url, retries=2)
        response = api.request("GET", "/x")

    assert state["hits"] == 3, "1 次原始 + 2 次重试"
    assert waits == [pytest.approx(1.0), pytest.approx(1.0)]
    assert response.status_code == 429
    assert api.last_response is response
    assert response.was_retried is True
    # 只有真正产出这个响应那一次的耗时能进 elapsed_ms，前面两次的等待不能累加
    assert response.elapsed_ms < 1000


def test_put_5xx_still_retries_with_backoff():
    """5xx 上没有 `Retry-After`：幂等方法的既有重试语义不受本次改造影响。"""
    with scripted_server([(503, {}), (200, {})]) as (base_url, state):
        api, waits = make_api(base_url, retries=1)
        response = api.request("PUT", "/x", json={"a": 1})

    assert state["hits"] == 2
    assert len(waits) == 1
    assert 0.5 <= waits[0] <= 0.625
    assert response.status_code == 200
