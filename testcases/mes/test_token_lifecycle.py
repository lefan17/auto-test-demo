"""P0-3 的验收用例：令牌的提前刷新、并发去重，以及 401 的幂等重放边界。

这一组用例刻意分成两层来看：
- **纯逻辑层**（不发请求）：用假的 `fetch` 驱动 `TokenProvider`，
  把"即将过期就刷新""并发只刷一次"变成可确定复现的断言。
  真实 TTL 下这些行为要靠等时钟，用例又慢又不稳。
- **真服务层**：起一个短 TTL 的 Mock 服务，验证 401 之后的行为边界
  —— 幂等方法被重放、非幂等方法**不**被重放。这是本次改造里唯一
  有"做错了会造脏数据"风险的改动，必须有负向对照。

关于为什么并发用例要断言"时间区间交叠"：
判断口径在 `concurrency.py`，那里的注释解释了为什么不能拿总耗时当判据
（后端用 BEGIN IMMEDIATE 串行化写请求，并发反而更慢）。
"""

from __future__ import annotations

import threading
import time

import pytest
from concurrency import assert_requests_overlapped, race_timeline
from mock_server_ctl import MockServer

from apis import MesApi
from apis.token_provider import LoginResult, TokenProvider

pytestmark = [pytest.mark.mes, pytest.mark.api, pytest.mark.regression]

# 服务端 TTL 取 1 秒时，"等到它真的过期"需要等多久。
# 不能只等 1.5 秒：`mock_server/security.py` 的 decode_token() 是拿
# `int(payload["exp"]) < int(time.time())` 判过期的，两端都截断到整秒，
# 最坏情况下多出接近 2 秒的余量。等 2.2 秒是把这个余量吃干净，
# 让"过期"成为确定事实而不是概率事件。
STALE_WAIT_SECONDS = 2.2


class _FakeLogin:
    """可计数的假登录：记录被调用了几次、每次给一个不同的令牌。"""

    def __init__(self, ttl: int = 3600):
        self.ttl = ttl
        self.calls = 0
        self.names: list[str] = []

    def __call__(self) -> LoginResult:
        self.calls += 1
        self.names.append(f"t{self.calls}-{threading.get_ident()}")
        return LoginResult(token=f"token-{self.calls}", expires_in=self.ttl, user={"username": "u", "role": "OPERATOR"})


# ---------- 纯逻辑层：提前刷新 ----------
def test_first_access_logs_in_once_and_caches():
    fetch = _FakeLogin()
    provider = TokenProvider(fetch, refresh_margin=1)
    assert provider.token == "token-1"
    assert provider.token == "token-1", "令牌还有效时不应重复登录"
    assert fetch.calls == 1


def test_refreshes_inside_the_margin_window():
    """进入提前刷新窗口就必须换令牌，而不是等它真的过期。

    为什么提前：过期与刷新之间所有并发请求都会吃一次 401；
    把刷新挪到过期之前，正常情况下外部看不到 401。
    """
    fetch = _FakeLogin(ttl=10)
    provider = TokenProvider(fetch, refresh_margin=60)  # 窗口比 TTL 还长 -> 永远"即将过期"
    assert provider.is_expiring() is True
    assert provider.token == "token-1"
    assert provider.token == "token-2", "已在刷新窗口内，第二次取值应换新令牌"
    assert fetch.calls == 2


def test_does_not_refresh_outside_the_margin_window():
    fetch = _FakeLogin(ttl=3600)
    provider = TokenProvider(fetch, refresh_margin=60)
    assert provider.token == "token-1"  # 先登录一次：从未登录时 is_expiring() 为 True
    assert provider.is_expiring() is False
    for _ in range(5):
        assert provider.token == "token-1"
    assert fetch.calls == 1, "窗口外反复取值不应触发登录"


def test_zero_margin_waits_until_expiry():
    """窗口取 0 表示"过期再换"：TTL 很短时才刷新。"""
    fetch = _FakeLogin(ttl=1)
    provider = TokenProvider(fetch, refresh_margin=0)
    assert provider.token == "token-1"
    assert provider.is_expiring() is False
    time.sleep(1.1)
    assert provider.token == "token-2"


def test_refresh_margin_reads_env(monkeypatch):
    monkeypatch.setenv("TOKEN_REFRESH_MARGIN_SECONDS", "7")
    provider = TokenProvider(_FakeLogin())
    assert provider.refresh_margin == 7


def test_expires_at_uses_server_ttl():
    """到期时刻由服务端返回的 ttl 推出，不自己写本地常量。"""
    fetch = _FakeLogin(ttl=120)
    provider = TokenProvider(fetch, refresh_margin=0)
    before = time.time()
    assert provider.token == "token-1"
    assert before + 120 <= provider.expires_at <= time.time() + 120


def test_force_refresh_ignores_local_clock():
    """服务端判了 401 时，必须能强制换令牌，哪怕本地认为它还新鲜。

    这是 401 重放的支点：本地时钟比服务端慢、令牌被提前吊销、
    TTL 两端不一致，都会让"本地还有效"和"服务端已失效"同时成立。
    """
    fetch = _FakeLogin(ttl=3600)
    provider = TokenProvider(fetch, refresh_margin=60)
    assert provider.token == "token-1"
    assert provider.is_expiring() is False
    assert provider.refresh() == "token-2"
    assert fetch.calls == 2


# ---------- 纯逻辑层：并发只刷一次 ----------
def test_concurrent_expired_access_logs_in_only_once():
    """多线程同时发现要过期时，只能有一次真正去登录。

    做法：用一个会阻塞的 fetch 把所有线程都卡在锁外面，
    保证"它们真的同时想刷新"，而不是碰运气。
    """
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def slow_fetch() -> LoginResult:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait(timeout=5)
        return LoginResult(token="fresh", expires_in=3600, user={"username": "u", "role": "OPERATOR"})

    provider = TokenProvider(slow_fetch, refresh_margin=60)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(provider.token)
        except BaseException as exc:  # noqa: BLE001 - 线程异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    assert started.wait(timeout=5), "没有任何线程进入 fetch，用例前提不成立"
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发取值抛异常: {errors}"
    assert calls == 1, f"8 个线程同时要过期，实际登录了 {calls} 次（应当只有 1 次）"
    assert results == ["fresh"] * 8, f"所有线程都应拿到同一个新令牌，实际 {results}"


def test_concurrent_first_access_logs_in_only_once():
    """首次取值也是并发场景：冷启动时一批请求同时进来。"""
    fetch = _FakeLogin(ttl=3600)
    provider = TokenProvider(fetch, refresh_margin=60)
    barrier = threading.Barrier(8, timeout=10)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()
            results.append(provider.token)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发取值抛异常: {errors}"
    assert fetch.calls == 1, f"并发冷启动实际登录了 {fetch.calls} 次（应当只有 1 次）"
    assert results == ["token-1"] * 8


def test_concurrent_invalidate_then_access_logs_in_only_once(race):
    """令牌被丢弃后，一批并发请求只应产生**一次**登录。

    这条测的是"二次确认"那道闸：如果进锁后不重新比对版本，
    每个已经判定"需要刷新"的线程都会各自登录一遍。
    用 `race` fixture 是为了证明这确实是并发发生的，不是排队跑出来的。
    """
    fetch = _FakeLogin(ttl=3600)
    provider = TokenProvider(fetch, refresh_margin=60)
    assert provider.token == "token-1"
    before = fetch.calls

    def invalidate_then_take(_=None) -> str:
        provider.invalidate()
        # 让两个线程都卡在"令牌已丢弃"这个状态上，确保它们都想刷新
        time.sleep(0.02)
        return provider.token

    results, elapsed_ms, spans = race_timeline(race([invalidate_then_take, invalidate_then_take]), workers=2)
    assert_requests_overlapped(spans, workers=2, elapsed_ms=elapsed_ms, action="并发首次取令牌")
    assert fetch.calls - before == 1, (
        f"两个线程同时发现令牌缺失，实际登录了 {fetch.calls - before} 次（应当只有 1 次）：{results}"
    )


# ---------- 真服务层：401 之后的边界 ----------
@pytest.fixture(scope="module")
def short_ttl_server(tmp_path_factory):
    """一个服务端 TTL 只有 1 秒的 Mock 服务，专门用来制造"真实的 401"。

    为什么不能复用会话级的 mes_server：TTL 是 Mock 进程启动时读进模块级常量的
    （`mock_server/security.py` 的 `TOKEN_TTL_SECONDS`），会话级服务只起一次、
    TTL 是 3600，改不了；服务起来之后再改环境变量对已加载的常量无效。
    所以只能在启动这一刻用 `extra_env` 传进去。
    """
    tmp_dir = tmp_path_factory.mktemp("short_ttl")
    server = MockServer(
        db_path=tmp_dir / "short_ttl.db",
        log_path=tmp_dir / "short_ttl.log",
        extra_env={"MOCK_TOKEN_TTL": "1"},
    )
    server.start(timeout=60.0)
    try:
        yield server
    finally:
        server.stop()


class OptimisticTtlMesApi(MesApi):
    """一个"按 3600 秒记 TTL"的客户端，用来对上只给 1 秒 TTL 的服务端。

    为什么需要它：真实世界最常见的那类 401 来自"两端 TTL 认知不一致"——
    网关按自己的配置签发，客户端按接口文档假设 3600。如果两边认知一致，
    provider 会在令牌过期**之前**就把它换掉，401 这条路径永远不会被走到，
    测试也就永远测不到"401 → 换令牌 → 重放"这段逻辑。
    把客户端的 TTL 假设改大，是在不引入假服务端的前提下复现这个错配。
    """

    ASSUMED_TTL = 3600

    def login_ok(self, username: str, password: str) -> LoginResult:
        result = super().login_ok(username, password)
        return LoginResult(token=result.token, expires_in=self.ASSUMED_TTL, user=result.user)


class BogusTokenMesApi(OptimisticTtlMesApi):
    """登录"成功"但拿回的令牌服务端一定不认，用来验证重放不会无限循环。"""

    def login_ok(self, username: str, password: str) -> LoginResult:
        result = super().login_ok(username, password)
        return LoginResult(token="not-a-valid-token", expires_in=self.ASSUMED_TTL, user=result.user)


def test_expired_token_is_rejected_then_reauthed_on_idempotent(short_ttl_server):
    """过期令牌遇上幂等请求：自动换令牌并重放成功，且在响应上留下 was_reauthed 标记。

    为什么必须留下标记：报告里"这条 200 是一次重放拿到的"和"一次就成功了"
    是完全不同的两件事，后者说明刷新逻辑根本没被走到。
    """
    api = OptimisticTtlMesApi(short_ttl_server.base_url, retries=0)
    client = api.as_role("ADMIN")

    first = client.get("/work-orders")
    assert first.status_code == 200, f"首次请求应当成功：{first.status_code} {first.text[:200]}"
    assert getattr(first, "was_reauthed", None) is False, "首次请求不该有重放标记"
    logins_after_first = client.provider.login_count

    time.sleep(STALE_WAIT_SECONDS)  # 服务端已判过期，客户端仍以为有效

    again = client.get("/work-orders")
    assert again.status_code == 200, f"过期后应当换令牌重放成功，实际 {again.status_code}：{again.text[:200]}"
    assert getattr(again, "was_reauthed", None) is True, (
        "这条响应来自换令牌后的重放，必须在响应上留下 was_reauthed 标记，否则报告里分不清"
    )
    assert client.provider.login_count == logins_after_first + 1, (
        f"应当只额外登录一次，实际多了 {client.provider.login_count - logins_after_first} 次"
    )


def test_expired_token_on_post_is_not_replayed(short_ttl_server):
    """负向对照：非幂等方法（POST）**不得**被自动重放。

    这是整个改动里唯一可能造脏数据的分支：一个"创建工单"的 POST 可能已经在
    服务端落库，只是响应回来时令牌恰好过期；自动重放会造出第二条工单。
    所以这里断言的正是"它没有被重放"——过期令牌下的 POST 必须把 401 直接
    交给用例，由人来决定怎么办。
    """
    api = OptimisticTtlMesApi(short_ttl_server.base_url, retries=0)
    client = api.as_role("PLANNER")
    assert client.get("/work-orders").status_code == 200
    logins_before = client.provider.login_count

    time.sleep(STALE_WAIT_SECONDS)

    resp = client.post(
        "/work-orders",
        json={"product_code": "FG-NOREPLAY", "planned_qty": "1.000", "owner": "张工"},
    )
    assert resp.status_code == 401, f"POST 不该被重放；期望直接 401，实际 {resp.status_code}：{resp.text[:300]}"
    assert getattr(resp, "was_reauthed", None) is False, "POST 不允许被标记为重放过"
    assert client.provider.login_count == logins_before, "POST 被拒后不该顺带刷新令牌"


def test_get_recovers_on_the_same_stale_token_that_post_cannot(short_ttl_server):
    """同一份过期令牌下，POST 不能自愈而 GET 可以——两者对照，排除"偶发成功"的解释。

    只看 POST 那条的话，"401"也可能是"请求根本没到服务端"之类的巧合；
    紧接着用**同一个 provider**发一次 GET，证明令牌确实处于可刷新状态、
    只是这份能力刻意不施加在非幂等方法上。
    """
    api = OptimisticTtlMesApi(short_ttl_server.base_url, retries=0)
    client = api.as_role("QA")
    assert client.get("/work-orders").status_code == 200
    time.sleep(STALE_WAIT_SECONDS)

    posted = client.post("/work-orders", json={})
    assert posted.status_code == 401, f"POST 期望 401，实际 {posted.status_code}"
    assert getattr(posted, "was_reauthed", None) is False

    fetched = client.get("/work-orders")
    assert fetched.status_code == 200, f"GET 应当自愈，实际 {fetched.status_code}：{fetched.text[:200]}"
    assert getattr(fetched, "was_reauthed", None) is True


def test_replay_happens_at_most_once_when_token_stays_invalid(short_ttl_server):
    """令牌一直被服务端拒绝时，重放必须只有一次，不能变成 401↔刷新的死循环。

    做法：让登录返回的令牌服务端一定不认，这样第一次 401 之后刷新也拿不到
    能用的令牌，第二次 401 必须**原样返回**而不是继续刷新。
    断言的落点是"登录次数只增加了 1 次"。
    """
    api = BogusTokenMesApi(short_ttl_server.base_url, retries=0)
    client = api.as_role("OPERATOR")  # 登录本身成功，只是拿回的令牌服务端不认
    logins_before = client.provider.login_count

    resp = client.get("/work-orders")
    assert resp.status_code == 401, f"服务端始终拒绝时应当把 401 返回给用例，实际 {resp.status_code}"
    assert client.provider.login_count - logins_before == 1, (
        f"重放最多一次：应当只额外登录 1 次，实际 {client.provider.login_count - logins_before} 次"
    )


def test_concurrent_401s_on_the_same_token_log_in_only_once(short_ttl_server, race):
    """并发吃到**同一个**过期令牌的 401 时，只允许登录一次。

    这是"并发只刷一次"在真服务上的那一半：纯逻辑层证明的是
    "同时发现令牌过期"的去重，而这条证明的是"同时被服务端判 401"的去重。
    两者不是同一段代码路径——前者进的是 `token` 属性的双重检查，
    后者进的是 401 分支的 `refresh_after()`。

    为什么必须用真实并发（race fixture）而不是顺序调用两次：
    顺序调用时第二个线程看到的是一个**已经被换掉**的令牌，无论如何
    都不会重复登录；只有让它们都拿着同一个过期令牌同时撞到 401，
    才能压出"每个线程各登录一次"这个缺陷。
    """
    api = OptimisticTtlMesApi(short_ttl_server.base_url, retries=0)
    client = api.as_role("ADMIN")
    assert client.get("/work-orders").status_code == 200
    time.sleep(STALE_WAIT_SECONDS)  # 服务端已判过期，客户端仍以为有效
    logins_before = client.provider.login_count

    def fetch_work_orders(_=None):
        return client.get("/work-orders")

    results, elapsed_ms, spans = race_timeline(race([fetch_work_orders] * 4), workers=4)
    assert_requests_overlapped(spans, workers=4, elapsed_ms=elapsed_ms, action="并发 401 重放")
    statuses = [r.status_code for r in results]
    assert statuses == [200] * 4, f"并发重放后都应当成功，实际 {statuses}：{[r.text[:120] for r in results]}"
    assert all(getattr(r, "was_reauthed", None) is True for r in results), (
        "4 条都应当是被重放救回来的（否则说明它们没走到 401 分支，这条用例就失去意义了）"
    )
    assert client.provider.login_count - logins_before == 1, (
        f"4 个线程撞同一个过期令牌，只应登录 1 次，实际 {client.provider.login_count - logins_before} 次"
    )
