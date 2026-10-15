"""端到端编排测试：load baseline → replay → diff → classify → 汇总 → 落库。

用本地 `http.server` 假服务端；`recorder.store` 相关用例做成「模块不可用即 skip」。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from analyze.classify import DiffCategory
from analyze.runner import (
    load_repository,
    make_baseline_id,
    replay_baseline,
    run_replay,
)
from replay.model import (
    IgnoreRules,
    RecordedExchange,
    RecordedRequest,
    RecordedResponse,
    make_exchange_id,
)
from replay.player import ExchangePlayer, ReplaySources

pytestmark = pytest.mark.analyze


# --------------------------------------------------------------------------
# 假服务端
# --------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def add(self, record: dict) -> None:
        with self.lock:
            self.calls.append(record)

    def paths(self) -> list[str]:
        with self.lock:
            return [c["path"] for c in self.calls]

    def last(self) -> dict:
        with self.lock:
            return self.calls[-1]


def _make_handler(routes: dict, recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def _handle(self) -> None:
            raw_path = self.path
            route = routes.get(raw_path.split("?")[0], {})
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
            self.send_response(route.get("status", 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    return Handler


@contextlib.contextmanager
def serve(routes: dict | None = None):
    recorder = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(routes or {}, recorder))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", recorder
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# 用例数据
# --------------------------------------------------------------------------


def baseline_exchange(
    eid: str, path: str, json_body: dict, method: str = "GET"
) -> RecordedExchange:
    request = RecordedRequest(
        method=method,
        url=f"http://recorded.invalid{path}",
        path=path,
        headers={"content-type": "application/json", "host": "recorded.invalid"},
        query={},
        body=None if method == "GET" else b"{}",
        body_json=None if method == "GET" else {},
    )
    return RecordedExchange(
        exchange_id=eid,
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        request=request,
        response=RecordedResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(json_body).encode(),
            body_json=json_body,
            elapsed_ms=12.0,
        ),
    )


# --------------------------------------------------------------------------
# run_replay：纯编排
# --------------------------------------------------------------------------


def test_run_replay_links_all_stages():
    routes = {
        "/api/items": {"json": {"qty": 1}},
        "/api/changed": {"json": {"qty": 999}},
        "/api/renamed": {"json": {"qty": 1, "extra": True}},
    }
    with serve(routes) as (base_url, _):
        exchanges = [
            baseline_exchange("e1", "/api/items", {"qty": 1}),
            baseline_exchange("e2", "/api/changed", {"qty": 1}),
            baseline_exchange("e3", "/api/renamed", {"qty": 1}),
        ]
        run = run_replay(
            exchanges, ExchangePlayer(base_url, concurrency=4, timeout=5.0),
            baseline_id="b-1",
        )

    assert run.report.total == 3
    assert run.matched == 3
    assert len(run.outcomes) == 3

    # e2 值回归 + e3 结构新增 = 2 个待评审接口
    assert run.risk.endpoints_with_diff == 2
    assert run.risk.total_raw_diffs == 2
    assert {i.path for i in run.risk.items} == {"/api/changed", "/api/renamed"}
    assert run.categories.counts[DiffCategory.REGRESSION.value] == 2
    assert run.categories.counts[DiffCategory.ENV_FAILURE.value] == 0
    assert {v.exchange_id for v in run.report.regressed} == {"e2", "e3"}
    assert [v.exchange_id for v in run.report.healthy] == ["e1"]


def test_run_replay_healthy_path_is_clean():
    with serve({"/api/items": {"json": {"qty": 1}}}) as (base_url, _):
        exchanges = [baseline_exchange("e1", "/api/items", {"qty": 1})]
        run = run_replay(exchanges, ExchangePlayer(base_url))

    assert run.matched == 1
    assert run.categories.counts[DiffCategory.CLEAN.value] == 1
    assert run.risk.endpoints_with_diff == 0


def test_run_replay_survives_transport_failure():
    """目标连不上时，整轮不中断，落到 ENV_FAILURE 且不进评审队列。"""
    player = ExchangePlayer("http://127.0.0.1:9", timeout=0.5)
    run = run_replay(
        [baseline_exchange("e1", "/api/items", {"qty": 1})], player, baseline_id="b"
    )

    assert run.categories.counts[DiffCategory.ENV_FAILURE.value] == 1
    assert run.risk.endpoints_with_diff == 0  # 环境失败不进评审队列
    assert run.verdicts[0].error is not None
    assert run.report.failed and run.report.failed[0].exchange_id == "e1"


def test_run_replay_applies_ignore_rules():
    with serve({"/api/items": {"json": {"qty": 1, "created_at": "2025-06-06T00:00:00Z"}}}) as (
        base_url,
        _,
    ):
        exchanges = [
            baseline_exchange(
                "e1", "/api/items", {"qty": 1, "created_at": "2024-01-01T00:00:00Z"}
            )
        ]
        run = run_replay(
            exchanges,
            ExchangePlayer(base_url),
            rules=IgnoreRules(ignored_paths=("$.created_at",)),
        )

    assert run.risk.diffs_before_noise == 0
    assert run.risk.endpoints_with_diff == 0
    assert run.categories.counts[DiffCategory.CLEAN.value] == 1


def test_run_replay_markdown_and_json_render():
    with serve({"/api/changed": {"json": {"qty": 999}}}) as (base_url, _):
        exchanges = [baseline_exchange("e1", "/api/changed", {"qty": 1})]
        run = run_replay(exchanges, ExchangePlayer(base_url), baseline_id="b-9")

    md = run.render_markdown()
    assert "# 回放差异报告" in md
    assert "`b-9`" in md
    assert "/api/changed" in md
    assert "0 条原始差异 -> 0 个有差异接口" not in md  # 确实有差异

    payload = json.loads(run.to_json())
    assert payload["baseline_id"] == "b-9"
    assert payload["endpoints_with_diff"] == 1
    assert payload["top"][0]["endpoint"] == "GET /api/changed"
    assert "qty" in "".join(payload["top"][0]["fields"])


def test_make_baseline_id_is_stable_and_handles_empty():
    exchanges = [baseline_exchange("e1", "/api/a", {}), baseline_exchange("e2", "/api/b", {})]
    assert make_baseline_id(exchanges) == "e1-2"
    assert make_baseline_id([]) == "empty"


def test_ignore_rules_volatile_fingerprints_select_policy():
    """显式白名单只启用点名的那条降噪规则。"""
    with serve({"/api/items": {"json": {"qty": 1, "id": 1002}}}) as (base_url, _):
        exchanges = [baseline_exchange("e1", "/api/items", {"qty": 1, "id": 1000})]
        run = run_replay(
            exchanges,
            ExchangePlayer(base_url),
            rules=IgnoreRules(volatile_value_fingerprints=("ping:id",)),
        )

    # 只启用了自增 ID 规则 -> id 差异降级为噪声，无待评审差异
    assert run.risk.endpoints_with_diff == 0
    assert run.categories.counts[DiffCategory.NOISE.value] == 1


# --------------------------------------------------------------------------
# 落库（依赖 recorder.store，不可用即 skip）
# --------------------------------------------------------------------------


def _repo_module():
    try:
        return importlib.import_module("recorder.store")
    except Exception:
        return None


def test_load_repository_returns_none_when_module_missing(monkeypatch):
    """recorder.store 不可用时必须降级，而不是把整条链路拖崩。"""
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "recorder.store":
            raise ImportError("模拟：录制端还没写好")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert load_repository(":memory:") is None


def test_replay_baseline_raises_when_store_unavailable(monkeypatch):
    monkeypatch.setattr("analyze.runner.load_repository", lambda _p: None)
    with pytest.raises(RuntimeError, match="recorder.store 不可用"):
        replay_baseline(":memory:", "b1", "http://127.0.0.1:1")


def test_replay_baseline_round_trips_through_sqlite(tmp_path: Path):
    """真落库路径：save_baseline → replay_baseline → save_report → latest_report。"""
    module = _repo_module()
    if module is None:
        pytest.skip("recorder.store 尚未可用")

    db = tmp_path / "traffic.sqlite3"
    routes = {"/api/items": {"json": {"qty": 1}}, "/api/changed": {"json": {"qty": 999}}}
    with serve(routes) as (base_url, _):
        with load_repository(db) as repo:
            baseline_id = repo.save_baseline(
                "smoke",
                [
                    baseline_exchange("e1", "/api/items", {"qty": 1}),
                    baseline_exchange("e2", "/api/changed", {"qty": 1}),
                ],
            )
            assert repo.load_baseline(baseline_id)[0].exchange_id == "e1"
        with load_repository(db) as repo:
            run = replay_baseline(
                db, baseline_id, base_url, repository=repo, concurrency=4, timeout=5.0
            )
            assert run.persisted is True
            assert run.report.total == 2
            assert run.risk.endpoints_with_diff == 1

            stored = repo.latest_report(baseline_id)
            assert stored is not None
            assert stored.baseline_id == baseline_id
            assert len(stored.verdicts) == 2
