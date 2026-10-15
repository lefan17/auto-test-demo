"""录制端测试：采样策略的确定性与各类筛选。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from recorder.capture import Sampler, SamplingPolicy, sample_exchanges
from recorder.source import LocalSynthetic
from replay.model import RecordedExchange, RecordedRequest, RecordedResponse

pytestmark = pytest.mark.recorder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_TWO64 = float(2**64)


def _exchange(
    n: int,
    path: str = "/api/v1/users",
    tags: dict[str, str] | None = None,
) -> RecordedExchange:
    return RecordedExchange(
        exchange_id=f"ex-{n:04d}",
        recorded_at=datetime(2025, 1, 6, 2, 15, 3, tzinfo=timezone.utc),
        request=RecordedRequest(
            method="GET",
            url=f"http://api.demo.local{path}",
            path=path,
            query={"n": [str(n)]},
            body=json.dumps({"n": n}).encode(),
        ),
        response=RecordedResponse(status=200),
        tags=tags or {"env": "staging"},
    )


def _many(count: int = 200, path: str = "/api/v1/users") -> list[RecordedExchange]:
    return [_exchange(i, path) for i in range(count)]


# -- 确定性 -----------------------------------------------------------------


def test_same_input_same_seed_same_output() -> None:
    policy = SamplingPolicy(sample_rate=0.35, seed=7)
    assert [e.exchange_id for e in sample_exchanges(_many(), policy)] == [
        e.exchange_id for e in sample_exchanges(_many(), policy)
    ]


def test_unit_interval_is_blake2b_not_python_hash() -> None:
    """采样随机数必须是 blake2b(seed|exchange_id)，不能用带进程随机化的 hash()。"""
    policy = SamplingPolicy(sample_rate=0.5, seed=11)
    exchange = _exchange(3)
    expected = int.from_bytes(
        hashlib.blake2b(f"11:{exchange.exchange_id}".encode(), digest_size=8).digest(), "big"
    ) / _TWO64
    assert Sampler(policy)._unit_interval(exchange) == pytest.approx(expected)


def test_different_seed_changes_the_draw_but_not_the_input() -> None:
    a = {e.exchange_id for e in sample_exchanges(_many(), SamplingPolicy(sample_rate=0.5, seed=1))}
    b = {e.exchange_id for e in sample_exchanges(_many(), SamplingPolicy(sample_rate=0.5, seed=2))}
    assert len(a) == len(b), "同比例下不同种子应保留同样条数"
    assert a != b, "不同种子应抽取不同样本"


def test_sampling_order_is_preserved() -> None:
    kept = [e.exchange_id for e in sample_exchanges(_many(), SamplingPolicy(sample_rate=0.5, seed=5))]
    assert kept == sorted(kept), "采样不得打乱原始顺序"


def test_samples_are_drawn_from_the_input() -> None:
    source = _many(50)
    source_ids = {e.exchange_id for e in source}
    kept = list(sample_exchanges(source, SamplingPolicy(sample_rate=0.5, seed=3)))
    assert {e.exchange_id for e in kept} <= source_ids
    assert all(any(e is s for s in source) for e in kept), "采样应原样透传对象，不重建"


# -- 比例 -------------------------------------------------------------------


def test_sample_rate_one_keeps_everything() -> None:
    assert len(list(sample_exchanges(_many(30), SamplingPolicy(sample_rate=1.0)))) == 30


def test_sample_rate_zero_drops_everything() -> None:
    assert list(sample_exchanges(_many(30), SamplingPolicy(sample_rate=0.0))) == []


def test_sample_rate_is_roughly_respected() -> None:
    kept = len(list(sample_exchanges(_many(2000), SamplingPolicy(sample_rate=0.25, seed=9))))
    assert 0.20 * 2000 < kept < 0.30 * 2000, kept


def test_negative_sample_rate_rejected() -> None:
    with pytest.raises(ValueError):
        Sampler(SamplingPolicy(sample_rate=-0.1))


# -- 路径权重 ---------------------------------------------------------------


def test_zero_weight_excludes_a_path() -> None:
    policy = SamplingPolicy(
        sample_rate=1.0,
        path_weights={"/api/v1/admin/audit-log": 0.0},
    )
    kept = list(sample_exchanges(_many(20), policy)) + list(
        sample_exchanges(_many(5, path="/api/v1/admin/audit-log"), policy)
    )
    assert all(e.request.path != "/api/v1/admin/audit-log" for e in kept)
    assert len(kept) == 20


def test_weight_two_is_effectively_always_kept_and_weaker_paths_drop() -> None:
    policy = SamplingPolicy(sample_rate=0.5, path_weights={"/api/v1/hot": 2.0, "/api/v1/cold": 0.0})
    hot = list(sample_exchanges(_many(40, path="/api/v1/hot"), policy))
    cold = list(sample_exchanges(_many(40, path="/api/v1/cold"), policy))
    assert len(hot) == 40
    assert cold == []


def test_prefix_wildcard_matches_and_longest_pattern_wins() -> None:
    policy = SamplingPolicy(
        path_weights={"/api/v1/admin/*": 0.0, "/api/v1/*": 1.0},
        default_weight=1.0,
    )
    sampler = Sampler(policy)
    assert sampler.weight_for("/api/v1/admin/audit-log") == 0.0
    assert sampler.weight_for("/api/v1/users") == 1.0
    assert sampler.weight_for("/api/v2/orders") == 1.0


def test_default_weight_applies_to_unlisted_paths() -> None:
    sampler = Sampler(SamplingPolicy(sample_rate=1.0, default_weight=0.0))
    assert sampler.weight_for("/whatever") == 0.0
    assert list(sampler.sample(_many(5))) == []


def test_probability_is_clamped_to_unit_interval() -> None:
    sampler = Sampler(SamplingPolicy(sample_rate=5.0))
    assert sampler.probability(_exchange(1)) == 1.0
    assert Sampler(SamplingPolicy(sample_rate=0.0)).probability(_exchange(1)) == 0.0


# -- 标签过滤 ---------------------------------------------------------------


def test_require_tags_keeps_only_matching() -> None:
    policy = SamplingPolicy(sample_rate=1.0, require_tags={"env": "prod"})
    exchanges = [_exchange(1, tags={"env": "prod"}), _exchange(2, tags={"env": "staging"})]
    assert [e.exchange_id for e in sample_exchanges(exchanges, policy)] == ["ex-0001"]


def test_require_tags_needs_all_of_them() -> None:
    policy = SamplingPolicy(sample_rate=1.0, require_tags={"env": "prod", "biz": "order"})
    exchanges = [
        _exchange(1, tags={"env": "prod", "biz": "order"}),
        _exchange(2, tags={"env": "prod", "biz": "user"}),
        _exchange(3, tags={"env": "prod"}),
    ]
    assert [e.exchange_id for e in sample_exchanges(exchanges, policy)] == ["ex-0001"]


def test_forbid_tags_drops_on_any_hit() -> None:
    policy = SamplingPolicy(sample_rate=1.0, forbid_tags={"noisy": "true"})
    exchanges = [_exchange(1), _exchange(2, tags={"env": "staging", "noisy": "true"})]
    assert [e.exchange_id for e in sample_exchanges(exchanges, policy)] == ["ex-0001"]


def test_forbid_tags_beats_require_tags() -> None:
    policy = SamplingPolicy(
        sample_rate=1.0, require_tags={"env": "staging"}, forbid_tags={"noisy": "true"}
    )
    exchanges = [_exchange(1, tags={"env": "staging"}), _exchange(2, tags={"env": "staging", "noisy": "true"})]
    assert [e.exchange_id for e in sample_exchanges(exchanges, policy)] == ["ex-0001"]


# -- 逐路径限流 -------------------------------------------------------------


def test_max_per_path_caps_high_frequency_endpoints() -> None:
    policy = SamplingPolicy(sample_rate=1.0, max_per_path=3)
    kept = list(sample_exchanges(_many(10), policy))
    assert len(kept) == 3
    assert [e.exchange_id for e in kept] == ["ex-0000", "ex-0001", "ex-0002"]


def test_max_per_path_is_per_path_not_global() -> None:
    policy = SamplingPolicy(sample_rate=1.0, max_per_path=2)
    exchanges = _many(5, path="/api/a") + _many(5, path="/api/b")
    kept = list(sample_exchanges(exchanges, policy))
    assert len(kept) == 4


# -- 与真实 fixture 串联 ----------------------------------------------------


def test_sampling_real_fixtures_is_deterministic_and_filterable() -> None:
    exchanges = list(LocalSynthetic(FIXTURES).iter_exchanges())
    policy = SamplingPolicy(sample_rate=1.0, forbid_tags={"noisy": "true"})
    once = [e.exchange_id for e in sample_exchanges(exchanges, policy)]
    twice = [e.exchange_id for e in sample_exchanges(exchanges, policy)]
    assert once == twice
    assert len(once) == len(exchanges) - 1, "fixtures 里只有 1 条带 noisy 标签"
