"""回放器测试：并发、QPS、头白名单、影子模式、单条失败隔离。

全部用本地 `http.server` 起假服务端，不依赖任何外部网络。
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from replay.model import (
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    make_exchange_id,
)
from replay.player import (
    DEFAULT_HEADER_ALLOWLIST,
    ExchangePlayer,
    ReplaySources,
    build_headers,
)

pytestmark = pytest.mark.replay


# --------------------------------------------------------------------------
# 假服务端
# --------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.current = 0
        self.max_concurrent = 0

    def enter(self) -> None:
        with self.lock:
            self.current += 1
            self.max_concurrent = max(self.max_concurrent, self.current)

    def leave(self) -> None:
        with self.lock:
            self.current -= 1

    def add(self, record: dict) -> None:
        with self.lock:
            self.calls.append(record)

    def last(self) -> dict:
        return self.calls[-1]


def _make_handler(routes: dict, recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # 静音
            pass

        def _handle(self) -> None:
            recorder.enter()
            try:
                raw_path = self.path
                route = routes.get(raw_path.split("?")[0], {})
                if route.get("delay"):
                    time.sleep(route["delay"])
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                recorder.add(
                    {
                        "method": self.command,
                        "path": raw_path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                payload = json.dumps(route.get("json", {"ok": True})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                recorder.leave()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    return Handler


@contextlib.contextmanager
def serve(routes: dict | None = None):
    recorder = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(routes or {}, recorder))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://127.0.0.1:{port}", recorder
    finally:
        server.shutdown()
        server.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------
# 用例数据
# --------------------------------------------------------------------------


def exchange(
    method: str = "GET",
    path: str = "/api/items",
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    query: dict[str, list[str]] | None = None,
) -> RecordedExchange:
    request = RecordedRequest(
        method=method,
        url=f"http://recorded.invalid{path}",
        path=path,
        headers=headers or {},
        query=query or {},
        body=body,
        body_json=json.loads(body) if body else None,
    )
    return RecordedExchange(
        exchange_id=make_exchange_id(request),
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        request=request,
        response=RecordedResponse(status=200, headers={"content-type": "application/json"}),
    )


# --------------------------------------------------------------------------
# 头白名单
# --------------------------------------------------------------------------


def test_whitelist_drops_host_content_length_and_authorization():
    """录制的 Host / Content-Length / Authorization 一律不透传。"""
    recorded = {
        "Host": "evil.example.com",
        "Content-Length": "999",
        "Authorization": "Bearer OLD-RECORDED-TOKEN",
        "Cookie": "session=stale",
        "Accept": "application/json",
        "User-Agent": "recorder/1.0",
        "X-Request-Id": "abc",
    }
    headers = build_headers("GET", recorded)

    assert "host" not in headers
    assert "content-length" not in headers
    assert "authorization" not in headers
    assert "cookie" not in headers
    # 白名单内的正常头照旧透传
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == "recorder/1.0"
    assert headers["x-request-id"] == "abc"
    assert set(headers) <= set(DEFAULT_HEADER_ALLOWLIST)


def test_token_is_injected_by_caller_not_replayed():
    headers = build_headers(
        "GET",
        {"Authorization": "Bearer OLD"},
        ReplaySources(headers_override={"Authorization": "Bearer NEW", "X-Env": "staging"}),
    )
    assert headers["authorization"] == "Bearer NEW"
    assert headers["x-env"] == "staging"


def test_override_cannot_smuggle_host_or_content_length():
    headers = build_headers(
        "GET", {}, ReplaySources(headers_override={"Host": "x", "Content-Length": "5"})
    )
    assert "host" not in headers
    assert "content-length" not in headers


def test_headers_actually_sent_are_rebuilt_by_transport():
    """端到端：服务端看到的 Host 是真实目标，Content-Length 是真实 body 长度。"""
    with serve({"/api/items": {"json": {"ok": True}}}) as (base_url, recorder):
        ex = exchange(
            "POST",
            "/api/items",
            headers={
                "Host": "evil.example.com",
                "Content-Length": "999",
                "Authorization": "Bearer OLD",
                "Accept": "application/json",
            },
            body=b'{"a":1}',
        )
        player = ExchangePlayer(base_url)
        outcomes = player.run([ex])

    assert outcomes[0].error is None
    sent = recorder.last()["headers"]
    assert sent["host"] == base_url.split("//", 1)[1]
    assert sent["content-length"] == str(len(b'{"a":1}'))
    assert "authorization" not in sent
    assert sent["accept"] == "application/json"


# --------------------------------------------------------------------------
# 影子模式
# --------------------------------------------------------------------------


def test_write_method_gets_shadow_headers():
    sources = ReplaySources(
        shadow_headers={"X-Shadow": "1", "X-Shadow-Target": "shadow-env"}
    )
    with serve({"/api/orders": {"json": {"ok": True}}}) as (base_url, recorder):
        player = ExchangePlayer(base_url, sources=sources)
        outcomes = player.run(
            [exchange("POST", "/api/orders", body=b'{"sku":"A1"}')]
        )

    assert outcomes[0].error is None
    sent = recorder.last()["headers"]
    assert recorder.last()["method"] == "POST"
    assert sent["x-shadow"] == "1"
    assert sent["x-shadow-target"] == "shadow-env"
    assert "host" not in sent or sent["host"] not in ("evil.example.com",)


def test_read_method_does_not_get_shadow_headers():
    sources = ReplaySources(shadow_headers={"X-Shadow": "1"})
    with serve({"/api/orders": {"json": {"ok": True}}}) as (base_url, recorder):
        player = ExchangePlayer(base_url, sources=sources)
        player.run([exchange("GET", "/api/orders")])

    assert "x-shadow" not in recorder.last()["headers"]


def test_write_method_still_sent_without_shadow_headers():
    """shadow_headers 为空时请求照发（由调用方保证目标可写）。"""
    with serve({"/api/orders": {"json": {"ok": True}}}) as (base_url, recorder):
        player = ExchangePlayer(base_url)
        outcomes = player.run([exchange("DELETE", "/api/orders")])

    assert outcomes[0].status == 200
    assert "x-shadow" not in recorder.last()["headers"]


# --------------------------------------------------------------------------
# 失败隔离
# --------------------------------------------------------------------------


def test_single_failure_does_not_break_the_round():
    with serve(
        {
            "/slow": {"delay": 1.2, "json": {"ok": True}},
            "/fast": {"json": {"ok": True}},
        }
    ) as (base_url, recorder):
        player = ExchangePlayer(base_url, timeout=0.3, concurrency=4)
        outcomes = player.run(
            [
                exchange("GET", "/fast"),
                exchange("GET", "/slow"),
                exchange("GET", "/fast", query={"page": ["2"]}),
            ]
        )

    assert len(outcomes) == 3
    failed = [o for o in outcomes if o.error is not None]
    assert len(failed) == 1
    assert "Timeout" in failed[0].error
    assert failed[0].status is None
    assert len([o for o in outcomes if o.status == 200]) == 2


def test_connect_error_is_reported_per_exchange():
    port = free_port()
    player = ExchangePlayer(f"http://127.0.0.1:{port}", timeout=1.0)
    outcomes = player.run([exchange("GET", "/api/x"), exchange("GET", "/api/y")])

    assert len(outcomes) == 2
    for outcome in outcomes:
        assert outcome.status is None
        assert outcome.error is not None
        assert "ConnectError" in outcome.error or "Connect" in outcome.error


def test_empty_input_returns_empty_tuple():
    player = ExchangePlayer("http://127.0.0.1:1")
    assert player.run([]) == ()


# --------------------------------------------------------------------------
# 并发与限流
# --------------------------------------------------------------------------


def test_concurrency_cap_is_respected():
    with serve({"/api/slow": {"delay": 0.15, "json": {"ok": True}}}) as (base_url, recorder):
        player = ExchangePlayer(base_url, concurrency=2, timeout=5.0)
        outcomes = player.run(
            [exchange("GET", "/api/slow", query={"i": [str(i)]}) for i in range(6)]
        )

    assert all(o.status == 200 for o in outcomes)
    assert recorder.max_concurrent <= 2


def test_requests_are_actually_parallel():
    """并行的硬证据是服务端同时处理的条数，不是墙钟——本机 AsyncClient 启动有 ~0.2s 固定开销。"""
    with serve({"/api/slow": {"delay": 0.25, "json": {"ok": True}}}) as (base_url, recorder):
        player = ExchangePlayer(base_url, concurrency=8, timeout=5.0)
        started = time.perf_counter()
        outcomes = player.run(
            [exchange("GET", "/api/slow", query={"i": [str(i)]}) for i in range(8)]
        )
        elapsed = time.perf_counter() - started

    assert all(o.status == 200 for o in outcomes)
    assert recorder.max_concurrent >= 4, (
        f"并发未生效：服务端同时只处理了 {recorder.max_concurrent} 条"
    )
    # 松上界：串行下限 8 * 0.25 = 2.0s
    assert elapsed < 1.5, f"8 条并发请求耗时 {elapsed:.2f}s，已接近串行下限 2.0s"


def test_qps_limiter_spaces_requests():
    with serve({"/api/x": {"json": {"ok": True}}}) as (base_url, _):
        player = ExchangePlayer(base_url, concurrency=8, qps=10.0, timeout=5.0)
        started = time.perf_counter()
        outcomes = player.run(
            [exchange("GET", "/api/x", query={"i": [str(i)]}) for i in range(6)]
        )
        elapsed = time.perf_counter() - started

    assert all(o.status == 200 for o in outcomes)
    # 6 条 @10qps：最小间隔 0.1s，首条不等待 => 至少 0.5s
    assert elapsed >= 0.45, f"限流未生效，实测 {elapsed:.2f}s"


def test_replay_is_async_capable():
    import asyncio

    with serve({"/api/x": {"json": {"ok": True}}}) as (base_url, _):
        player = ExchangePlayer(base_url)
        outcomes = asyncio.run(player.replay([exchange("GET", "/api/x")]))

    assert outcomes[0].status == 200


def test_base_url_is_required():
    with pytest.raises(ValueError):
        ExchangePlayer("")
