"""流量源抽象：录制端唯一的「流量从哪来」接口。

两个实现共享同一个出口形状 —— `iter_exchanges() -> Iterator[RecordedExchange]`，
因此采样、脱敏、入库与回放端都不需要知道流量是合成的还是旁路抓的。

`ProxyTap` 是懒加载接入点：`mitmproxy` 未安装时模块顶层不得 import 它，
否则本地（无 mitmproxy）链路会直接崩在 import 阶段。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from replay.model import (
    Dependency,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    make_exchange_id,
    normalize_headers,
)

__all__ = ["MITMPROXY_HINT", "LocalSynthetic", "ProxyTap", "TrafficSource", "flow_to_exchange"]

MITMPROXY_HINT = "未安装 mitmproxy，ProxyTap 旁路接入不可用；需 pip install mitmproxy"


class TrafficSource(ABC):
    """流量源契约：任何来源都只暴露一个迭代器出口。"""

    @abstractmethod
    def iter_exchanges(self) -> Iterator[RecordedExchange]:
        """产出录制交换；实现方自行决定顺序，但必须可重复（同输入同输出）。"""
        raise NotImplementedError


# --------------------------------------------------------------------------
# 本地合成流量
# --------------------------------------------------------------------------


class LocalSynthetic(TrafficSource):
    """读取 `fixtures/` 下的 JSON 合成流量。

    文件结构（`exchanges` 也可直接是一个数组）::

        {"exchanges": [{"recorded_at": "...", "request": {...}, "response": {...},
                        "tags": {...}, "deps": [...]}]}
    """

    def __init__(self, fixtures_dir: str | Path, *, pattern: str = "*.json") -> None:
        self.fixtures_dir = Path(fixtures_dir)
        self.pattern = pattern
        if not self.fixtures_dir.is_dir():
            raise FileNotFoundError(f"fixtures 目录不存在: {self.fixtures_dir}")

    def iter_exchanges(self) -> Iterator[RecordedExchange]:
        for path in sorted(self.fixtures_dir.glob(self.pattern)):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for raw in _raw_exchanges(payload):
                yield _build_exchange(raw, origin=path.name)


def _raw_exchanges(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("exchanges", [])
    else:
        raw = payload
    if not isinstance(raw, list):
        raise ValueError("fixtures 文件的 exchanges 必须是数组")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("fixtures 文件里的每条交换必须是对象")
        yield item


def _build_exchange(raw: dict[str, Any], *, origin: str) -> RecordedExchange:
    req = raw["request"]
    res = raw.get("response") or {}
    url = str(req["url"])
    split = urlsplit(url)

    explicit_query = req.get("query")
    if explicit_query:
        query = {str(k): [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in explicit_query.items()}
    else:
        query = {k: list(v) for k, v in parse_qs(split.query, keep_blank_values=True).items()}

    req_body_json = req.get("body_json")
    res_body_json = res.get("body_json")
    request = RecordedRequest(
        method=str(req["method"]).upper(),
        url=url,
        path=split.path or "/",
        headers=normalize_headers({str(k): str(v) for k, v in (req.get("headers") or {}).items()}),
        query=query,
        body=_body_bytes(req.get("body"), req_body_json),
        body_json=req_body_json,
    )
    response = RecordedResponse(
        status=int(res.get("status", 200)),
        headers=normalize_headers({str(k): str(v) for k, v in (res.get("headers") or {}).items()}),
        body=_body_bytes(res.get("body"), res_body_json),
        body_json=res_body_json,
        elapsed_ms=None if res.get("elapsed_ms") is None else float(res["elapsed_ms"]),
    )
    return RecordedExchange(
        exchange_id=str(raw.get("exchange_id") or make_exchange_id(request)),
        recorded_at=_parse_time(raw.get("recorded_at")),
        request=request,
        response=response,
        tags=_tags(raw.get("tags"), origin=origin),
        deps=_deps(raw.get("deps")),
    )


def _body_bytes(raw_body: Any, body_json: Any) -> bytes | None:
    """`body` 缺失但给了 `body_json` 时，按同一 json 序列化口径补出字节。"""
    if raw_body is None:
        if body_json is None:
            return None
        return json.dumps(body_json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    raise TypeError(f"body 只支持字符串或省略，收到 {type(raw_body).__name__}")


def _parse_time(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _tags(raw: Any, *, origin: str) -> dict[str, str]:
    tags = {str(k): str(v) for k, v in (raw or {}).items()}
    tags.setdefault("source", "local-synthetic")
    tags.setdefault("fixture", origin)
    return tags


def _deps(raw: Any) -> tuple[Dependency, ...]:
    return tuple(
        Dependency(kind=str(d["kind"]), ref=str(d["ref"]), scrubbed=str(d["scrubbed"]))
        for d in (raw or ())
    )


# --------------------------------------------------------------------------
# mitmproxy 旁路接入点（懒加载）
# --------------------------------------------------------------------------


def _require_mitmproxy() -> Any:
    """函数体内 import：mitmproxy 未安装时不得炸掉模块导入。"""
    try:
        import mitmproxy  # noqa: PLC0415  仅为可用性探测
    except ImportError as exc:  # pragma: no cover - 本地必然命中
        raise RuntimeError(f"{MITMPROXY_HINT}（原始错误: {exc}）") from exc
    return mitmproxy


class ProxyTap(TrafficSource):
    """mitmproxy 旁路接入点。

    - 构造即校验 mitmproxy 可用性，缺依赖直接 `RuntimeError`（信息里带安装提示）。
    - `add_flow()` 收流（addon 或测试伪造），`iter_exchanges()` 把 flow 转成 `RecordedExchange`。
    - `run()` 起 DumpMaster 阻塞抓包，同样把 mitmproxy 的 import 关在函数体内。
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8899, flows: Iterable[Any] | None = None) -> None:
        self._mitmproxy = _require_mitmproxy()
        self.host = host
        self.port = port
        self._flows: list[Any] = list(flows or ())

    def add_flow(self, flow: Any) -> None:
        self._flows.append(flow)

    @property
    def captured(self) -> int:
        return len(self._flows)

    def iter_exchanges(self) -> Iterator[RecordedExchange]:
        for flow in self._flows:
            yield flow_to_exchange(flow)

    def run(self) -> None:
        """起 DumpMaster 阻塞抓包；addon 把每个完成的 flow 收进缓冲。"""
        from mitmproxy import options  # noqa: PLC0415
        from mitmproxy.tools.dump import DumpMaster  # noqa: PLC0415

        master = DumpMaster(options.Options(listen_host=self.host, listen_port=self.port))
        master.addons.add(_TapAddon(self))
        try:
            master.run()
        finally:
            master.shutdown()


class _TapAddon:
    """最小 addon：只关心响应完成后的 flow。"""

    def __init__(self, tap: ProxyTap) -> None:
        self._tap = tap

    def response(self, flow: Any) -> None:  # mitmproxy addon 钩子
        self._tap.add_flow(flow)


def flow_to_exchange(flow: Any) -> RecordedExchange:
    """mitmproxy `HTTPFlow` → `RecordedExchange`（鸭子类型，便于测试伪造）。"""
    req = flow.request
    res = getattr(flow, "response", None)
    url = str(getattr(req, "pretty_url", None) or req.url)
    split = urlsplit(url)
    headers_raw = getattr(req, "headers", None)
    headers = normalize_headers({str(k): str(v) for k, v in (headers_raw.items() if headers_raw else ())})
    body = getattr(req, "content", None)
    request = RecordedRequest(
        method=str(req.method).upper(),
        url=url,
        path=split.path or "/",
        headers=headers,
        query={k: list(v) for k, v in parse_qs(split.query, keep_blank_values=True).items()},
        body=body,
        body_json=_maybe_json(body, headers),
    )
    res_headers_raw = getattr(res, "headers", None) if res is not None else None
    res_body = getattr(res, "content", None) if res is not None else None
    res_headers = normalize_headers({str(k): str(v) for k, v in (res_headers_raw.items() if res_headers_raw else ())})
    start = getattr(req, "timestamp_start", None)
    end = getattr(res, "timestamp_end", None) if res is not None else None
    elapsed = None if (start is None or end is None) else round((float(end) - float(start)) * 1000, 3)
    response = RecordedResponse(
        status=int(getattr(res, "status_code", 0) or 0),
        headers=res_headers,
        body=res_body,
        body_json=_maybe_json(res_body, res_headers),
        elapsed_ms=elapsed,
    )
    tags = {"source": "proxy-tap", "host": split.netloc}
    return RecordedExchange(
        exchange_id=make_exchange_id(request),
        recorded_at=_parse_time(getattr(req, "timestamp_start", None)),
        request=request,
        response=response,
        tags=tags,
    )


def _maybe_json(body: bytes | None, headers: dict[str, str]) -> Any:
    if not body:
        return None
    ctype = headers.get("content-type", "")
    if "json" not in ctype.lower():
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
