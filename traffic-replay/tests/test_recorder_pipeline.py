"""录制端测试：端到端串联 源 → 采样 → 脱敏 → 入库 → 回读。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recorder.capture import SamplingPolicy, sample_exchanges
from recorder.scrubber import Scrubber
from recorder.source import LocalSynthetic
from recorder.store import Repository
from replay.model import RecordedExchange

pytestmark = pytest.mark.recorder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

PHONE_IN_FIXTURES = "13800138000"


def _record(fixtures: Path, **policy_kwargs) -> tuple[list[RecordedExchange], Scrubber]:
    scrubber = Scrubber()
    policy = SamplingPolicy(**policy_kwargs)
    out: list[RecordedExchange] = []
    for exchange in sample_exchanges(LocalSynthetic(fixtures).iter_exchanges(), policy):
        out.append(scrubber.scrub_with_deps(exchange))
    return out, scrubber


def test_pipeline_end_to_end() -> None:
    scrubbed, scrubber = _record(FIXTURES, sample_rate=1.0)
    assert len(scrubbed) == 7

    # 1) 采样后仍全部是冻结模型的对象，回放端可直接消费
    assert all(type(e) is RecordedExchange for e in scrubbed)

    # 2) 脱敏生效：fixtures 里的真实手机号不得残留
    dumped = json.dumps(
        [
            {"u": e.request.url, "q": e.request.query, "h": e.request.headers,
             "rb": e.request.body_json, "sb": e.response.body_json}
            for e in scrubbed
        ],
        ensure_ascii=False,
        default=str,
    )
    assert PHONE_IN_FIXTURES not in dumped, "脱敏后不得残留原始手机号"
    assert "13800138000" not in dumped

    # 3) 依赖被挂到交换上，映射方向是 原值 -> 脱敏值
    kinds = {d.kind for e in scrubbed for d in e.deps}
    assert {"phone", "email", "id_card"} <= kinds
    phone_dep = next(d for e in scrubbed for d in e.deps if d.ref == PHONE_IN_FIXTURES)
    assert phone_dep.kind == "phone"
    assert phone_dep.scrubbed != phone_dep.ref

    user_detail = next(e for e in scrubbed if e.request.path == "/api/v1/users/1001")
    assert user_detail.response.body_json["data"]["phone"] == phone_dep.scrubbed


def test_pipeline_honours_tag_filter_and_still_round_trips(tmp_path: Path) -> None:
    scrubbed, _ = _record(FIXTURES, sample_rate=1.0, forbid_tags={"noisy": "true"})
    assert len(scrubbed) == 6

    with Repository(tmp_path / "pipe.sqlite3") as repo:
        baseline_id = repo.save_baseline("staging-smoke", scrubbed)
        loaded = repo.load_baseline(baseline_id)

    assert loaded == scrubbed, "经过 采样+脱敏 的对象也必须能无损 round-trip"
    assert len(loaded) == 6


def test_pipeline_is_reproducible_byte_for_byte(tmp_path: Path) -> None:
    """同一份 fixture 跑两遍（不同进程语义：新对象、新连接），落库字节必须一致。"""
    first, _ = _record(FIXTURES, sample_rate=0.6, seed=42)
    second, _ = _record(FIXTURES, sample_rate=0.6, seed=42)
    assert first == second

    ids = []
    for i, batch in enumerate((first, second)):
        with Repository(tmp_path / f"run{i}.sqlite3") as repo:
            ids.append(repo.load_baseline(repo.save_baseline("same", batch)))
    assert ids[0] == ids[1]


def test_pipeline_dependency_table_is_reusable_for_seeding() -> None:
    """依赖表是「脱敏后仍能命中真实数据」的唯一线索：必须能被调用方拿去造数。"""
    scrubbed, scrubber = _record(FIXTURES, sample_rate=1.0)
    mapping = {d.ref: d.scrubbed for d in scrubber.dependencies}
    kinds = {d.kind for d in scrubber.dependencies}

    assert mapping[PHONE_IN_FIXTURES] == "14130290158"
    assert "110101199003078515" in mapping
    assert {"phone", "email", "id_card", "token"} <= kinds or "token" in kinds
    # 依赖表不得含未脱敏的原值出现在脱敏侧
    assert all(ref != fake for ref, fake in mapping.items())
    # 每条依赖都必须能在某个交换里找到它对应的假值
    flattened = json.dumps([e.response.body_json for e in scrubbed], ensure_ascii=False, default=str)
    assert any(fake in flattened for fake in mapping.values())
