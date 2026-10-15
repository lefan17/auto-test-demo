"""回放端：并发回放、QPS 限流、影子模式。

设计要点（面试可张口就来的部分）：

1. **录制时的认证信息一律不可信**。录下来的 `Host` / `Content-Length` /
   `Authorization` 指向的是录制环境，回放时原样透传会把请求打到错误的主机、
   body 与长度不符、或者拿旧 token 撞 401。所以请求头走**白名单**，
   token 由调用方通过 `ReplaySources.headers_override` 显式注入。
2. **QPS 用信号量 + 最小间隔**实现，不引第三方限流库：信号量管并发上限，
   一把锁 + `next_at` 时间戳管「两次发起请求之间至少隔 1/qps 秒」。
3. **单条失败不得中断整轮**。每条 exchange 的异常都在 `_one()` 内被吞成
   带 `error` 的 `ReplayOutcome`，整轮结果永远是「条数 == 输入条数」。
4. **写方法默认影子模式**：POST/PUT/PATCH/DELETE 附加影子路由头，
   让流量落到影子环境而不是生产。
5. **`trust_env=False`**：回放目标由 `base_url` 显式给定，绝不继承开发机上的
   `HTTP_PROXY` / `NO_PROXY`。除了「回放不该悄悄走代理」这个语义理由，还因为
   `NO_PROXY` 里出现 `[::1]` 这类 IPv6 字面量时 httpx 会直接抛
   `InvalidURL: Invalid port`——那是环境噪音，不该让整轮回放崩掉。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

import httpx

from replay.model import (
    Headers,
    RecordedExchange,
    ReplayOutcome,
    normalize_headers,
)

# --------------------------------------------------------------------------
# 请求头策略
# --------------------------------------------------------------------------

#: 允许从录制流量里透传的请求头（其余一律丢弃）。
DEFAULT_HEADER_ALLOWLIST: tuple[str, ...] = (
    "accept",
    "accept-language",
    "content-type",
    "user-agent",
    "x-api-version",
    "x-app-id",
    "x-correlation-id",
    "x-request-id",
    "x-trace-id",
)

#: 即便出现在白名单或 override 里也禁止原样透传的头。
#: `host` 与 `content-length` 由 httpx 按真实目标与真实 body 重新计算，
#: 漏掉它们会直接导致请求打到错误主机或 body 被截断。
FORBIDDEN_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "content-length",
        "authorization",
        "proxy-authorization",
        "cookie",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "expect",
        "content-encoding",
    }
)

#: 由调用方注入的头里，仍然不允许出现的（必须由传输层自己算）。
_NEVER_INJECTABLE: frozenset[str] = frozenset({"host", "content-length"})

WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class ReplaySources:
    """回放上下文：调用方必须重新注入的运行时信息。

    - `headers_override`：覆盖/追加请求头，用于注入新环境的 token 等。
    - `shadow_headers`：非空时，写方法会附加这些头把请求路由到影子环境。
    - `header_allowlist`：录制流量可透传的头白名单。
    """

    headers_override: Headers = field(default_factory=dict)
    shadow_headers: Headers = field(default_factory=dict)
    header_allowlist: tuple[str, ...] = DEFAULT_HEADER_ALLOWLIST


def build_headers(
    method: str,
    recorded: Headers | None,
    sources: ReplaySources | None = None,
) -> Headers:
    """构造实际发出的请求头：录制头过白名单 → 注入 override → 附加影子头。"""
    src = sources or ReplaySources()
    allowed = {h.lower() for h in src.header_allowlist} - FORBIDDEN_HEADERS

    headers: Headers = {}
    for name, value in normalize_headers(recorded or {}).items():
        if name in allowed:
            headers[name] = value

    # override 允许注入 Authorization（token 走这里），但仍不许自带 Host/Content-Length。
    for name, value in normalize_headers(src.headers_override).items():
        if name in _NEVER_INJECTABLE:
            continue
        headers[name] = value

    if method.upper() in WRITE_METHODS:
        for name, value in normalize_headers(src.shadow_headers).items():
            if name in _NEVER_INJECTABLE:
                continue
            headers[name] = value

    return headers


def build_params(query: dict[str, list[str]] | None) -> list[tuple[str, str]]:
    """把录制的 query（多值字典）摊平成 httpx 接受的参数列表。"""
    if not query:
        return []
    return [(k, v) for k, values in query.items() for v in values]


def body_bytes(request) -> bytes | None:
    """优先用录制原始 bytes；没有就按 JSON 重新序列化。"""
    if request.body is not None:
        return request.body
    if request.body_json is not None:
        return json.dumps(request.body_json, ensure_ascii=False).encode("utf-8")
    return None


def _is_json_content(headers: Headers) -> bool:
    return "json" in normalize_headers(headers).get("content-type", "").lower()


# --------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------


class QpsLimiter:
    """信号量管并发、最小间隔管 QPS。qps<=0/None 表示不限速。"""

    def __init__(self, qps: float | None) -> None:
        self.min_interval = 1.0 / qps if qps and qps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.min_interval


# --------------------------------------------------------------------------
# 回放器
# --------------------------------------------------------------------------


class ExchangePlayer:
    """按 FROZEN_INTERFACES.md §3 的签名实现。"""

    def __init__(
        self,
        base_url: str,
        *,
        concurrency: int = 8,
        qps: float | None = None,
        timeout: float = 15.0,
        sources: "ReplaySources | None" = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 必填：回放目标必须是显式给定的环境")
        self.base_url = base_url.rstrip("/")
        self.concurrency = max(1, int(concurrency))
        self.qps = qps
        self.timeout = timeout
        self.sources = sources or ReplaySources()

    # -- 核心 ---------------------------------------------------------------

    async def replay(
        self, exchanges: Sequence[RecordedExchange]
    ) -> tuple[ReplayOutcome, ...]:
        items = list(exchanges)
        if not items:
            return ()
        semaphore = asyncio.Semaphore(self.concurrency)
        limiter = QpsLimiter(self.qps)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False, trust_env=False
        ) as client:
            tasks = [
                self._one(client, semaphore, limiter, ex) for ex in items
            ]
            # _one() 内部全量兜底，gather 不会因单条失败而抛。
            return tuple(await asyncio.gather(*tasks))

    def run(self, exchanges: Sequence[RecordedExchange]) -> tuple[ReplayOutcome, ...]:
        """同步包装：没有事件循环时直接 asyncio.run，否则另起线程。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.replay(exchanges))
        return _run_coro_in_thread(self.replay(exchanges))

    # -- 单条 ---------------------------------------------------------------

    async def _one(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        limiter: QpsLimiter,
        exchange: RecordedExchange,
    ) -> ReplayOutcome:
        started = time.perf_counter()
        request = exchange.request
        try:
            method = request.method.upper()
            headers = build_headers(method, request.headers, self.sources)
            url = self.base_url + request.path
            async with semaphore:
                await limiter.wait()
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=build_params(request.query),
                    content=body_bytes(request),
                )
            payload = response.content
            return ReplayOutcome(
                exchange_id=exchange.exchange_id,
                status=response.status_code,
                headers=normalize_headers(dict(response.headers)),
                body=payload,
                body_json=_parse_json(payload, response.headers),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:  # 超时、连接错、DNS、协议错…… 全部降级为单条失败
            return ReplayOutcome(
                exchange_id=exchange.exchange_id,
                status=None,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
            )


def _parse_json(body: bytes | None, headers: Headers) -> object:
    if not body or not _is_json_content(headers):
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _run_coro_in_thread(coro):
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 原样在调用线程重抛
            box["error"] = exc

    thread = threading.Thread(target=target, name="exchange-player-run")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]
