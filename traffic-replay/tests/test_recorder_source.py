"""录制端测试：流量源（本地合成 + mitmproxy 懒加载旁路）。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from recorder.source import LocalSynthetic, ProxyTap, flow_to_exchange
from replay.model import RecordedExchange

pytestmark = pytest.mark.recorder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _fixture_sources() -> list[RecordedExchange]:
    return list(LocalSynthetic(FIXTURES).iter_exchanges())


def test_local_synthetic_reads_all_fixtures() -> None:
    exchanges = _fixture_sources()
    assert len(exchanges) == 7, f"fixtures 应产出 7 条交换，实际 {len(exchanges)}"
    assert all(e.request.method == e.request.method.upper() for e in exchanges)


def test_local_synthetic_is_deterministic() -> None:
    first = [_key(e) for e in _fixture_sources()]
    second = [_key(e) for e in _fixture_sources()]
    assert first == second


def _key(e: RecordedExchange) -> tuple[str, str, str, str]:
    return (e.exchange_id, e.request.method, e.request.path, e.recorded_at.isoformat())


def test_local_synthetic_covers_required_scenarios() -> None:
    exchanges = _fixture_sources()
    methods = {e.request.method for e in exchanges}
    assert {"GET", "POST", "DELETE"} <= methods
    assert any("page=" in e.request.url for e in exchanges), "缺少分页列表场景"
    assert any(e.response.status == 201 for e in exchanges), "缺少 POST 创建场景"
    assert any(e.response.status == 204 for e in exchanges), "缺少空响应体场景"
    assert all(e.recorded_at.tzinfo is not None for e in exchanges), "recorded_at 必须 tz-aware"


def test_local_synthetic_normalizes_headers_to_lowercase() -> None:
    for exchange in _fixture_sources():
        for header in ("request", "response"):
            mapping = exchange.request.headers if header == "request" else exchange.response.headers
            assert all(k == k.lower() for k in mapping), f"{header} headers 未归一化: {mapping}"


def test_local_synthetic_missing_dir_raises() -> None:
    with pytest.raises(FileNotFoundError):
        LocalSynthetic(FIXTURES / "does-not-exist")


def test_local_synthetic_body_json_serialized_to_bytes() -> None:
    post = next(
        e for e in _fixture_sources() if e.request.method == "POST" and e.request.path == "/api/v1/users"
    )
    assert post.request.body is not None
    assert b'"phone"' in post.request.body
    assert post.request.body_json["phone"] == "13711112222"


# -- mitmproxy 懒加载 -------------------------------------------------------


def test_top_level_import_does_not_require_mitmproxy() -> None:
    """模块顶层不得 import mitmproxy：本机未安装，import 仍必须成功。"""
    assert "mitmproxy" not in sys.modules
    import recorder.source  # noqa: F401

    assert "mitmproxy" not in sys.modules


def test_proxy_tap_without_mitmproxy_raises_runtime_error() -> None:
    if "mitmproxy" in sys.modules:  # pragma: no cover - 本机未安装
        pytest.skip("本机已安装 mitmproxy，无法验证缺依赖路径")
    with pytest.raises(RuntimeError) as excinfo:
        ProxyTap(host="127.0.0.1", port=8899)
    message = str(excinfo.value)
    assert "pip install mitmproxy" in message
    print(f"\n[ProxyTap 未安装 mitmproxy 时的真实报错] {message}")


@dataclass
class _FakeHeaders:
    data: dict[str, str]

    def items(self):  # noqa: ANN201 - 模仿 mitmproxy Headers
        return self.data.items()


@dataclass
class _FakeMessage:
    url: str
    method: str = "GET"
    headers: dict[str, str] | None = None
    content: bytes | None = None


@dataclass
class _FakeRequest(_FakeMessage):
    timestamp_start: float | None = 1736123303.0


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] | None = None
    content: bytes | None = None
    timestamp_end: float | None = 1736123303.042


@dataclass
class _FakeFlow:
    request: _FakeRequest
    response: _FakeResponse | None


def _fake_flow() -> _FakeFlow:
    return _FakeFlow(
        request=_FakeRequest(
            url="http://api.demo.local/api/v1/users?page=1&size=10",
            method="get",
            headers={"Accept": "application/json"},
            content=None,
        ),
        response=_FakeResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=b'{"code":0,"data":{"id":7}}',
        ),
    )


def test_flow_to_exchange_maps_proxy_flow() -> None:
    exchange = flow_to_exchange(_fake_flow())
    assert exchange.request.method == "GET"
    assert exchange.request.path == "/api/v1/users"
    assert exchange.request.query == {"page": ["1"], "size": ["10"]}
    assert exchange.response.status == 200
    assert exchange.response.body_json == {"code": 0, "data": {"id": 7}}
    assert exchange.response.elapsed_ms == pytest.approx(42.0)
    assert exchange.tags["source"] == "proxy-tap"


def test_proxy_tap_iterates_injected_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("recorder.source._require_mitmproxy", lambda: object())
    tap = ProxyTap(host="127.0.0.1", port=8899)
    assert tap.captured == 0
    tap.add_flow(_fake_flow())
    exchanges = list(tap.iter_exchanges())
    assert len(exchanges) == 1
    assert exchanges[0].exchange_id == flow_to_exchange(_fake_flow()).exchange_id


def test_sources_are_interchangeable(monkeypatch: pytest.MonkeyPatch) -> None:
    """结构性约束：回放端对流量来源零感知 —— 两个实现出口类型完全一致。"""
    monkeypatch.setattr("recorder.source._require_mitmproxy", lambda: object())
    local = LocalSynthetic(FIXTURES)
    tap = ProxyTap(host="127.0.0.1", port=8899)
    tap.add_flow(_fake_flow())

    for source in (local, tap):
        exchanges = list(source.iter_exchanges())
        assert exchanges, f"{type(source).__name__} 未产出任何流量"
        for exchange in exchanges:
            assert type(exchange) is RecordedExchange
        assert callable(source.iter_exchanges)
