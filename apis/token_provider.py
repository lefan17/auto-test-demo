"""令牌生命周期：持有令牌与到期时间，负责提前刷新与并发去重。

要解决的问题（改造前 `MesApi.as_role()` 的行为）：
令牌在第一次登录时拿一次、之后一直用到进程结束。Mock 服务的令牌有 TTL，
过期的表现为"某几条用例偶发 401"，而不是一个确定的失败 —— 这类问题的定位成本
远高于它的成因，所以把它收口到一个有明确状态的 provider 里：

1. **提前刷新**：不等 401，而是在离到期还有 `refresh_margin` 秒时就换新的。
   为什么要提前而不是"过期了再刷"：从发现过期到重新登录之间，所有并发中的请求
   都会吃一次 401；提前刷新把这个窗口整体挪到过期之前，正常情况下外部看不到 401。
   窗口大小可配（环境变量 `TOKEN_REFRESH_MARGIN_SECONDS`，默认 60 秒）——
   机器时钟偏差、服务端 TTL 比客户端预期短、网络慢这几种情况都需要放大它。
2. **并发只刷一次**：多线程同时发现要过期时，只有第一个真正去登录，
   其余用它刷出来的结果。做法是锁 + 双重检查，且进锁后用 `_generation` 再确认一次
   "我进来之前看到的那个令牌还是当前的" —— 只有检查、没有第二次确认的话，
   第二个线程仍会重复登录（它进锁前就已经判定"需要刷新"了）。
   为什么这件事必须自证：重复登录在 Mock 上只是多一个请求，
   在真实系统里会打爆登录接口的限流，且把审计日志搅乱。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# 提前刷新窗口（秒）。默认 60：常见网关的令牌 TTL 是 3600，
# 留 60 秒既足够覆盖一次登录+重放，又不至于每次请求都触发刷新。
DEFAULT_REFRESH_MARGIN_SECONDS = 60
REFRESH_MARGIN_ENV = "TOKEN_REFRESH_MARGIN_SECONDS"


class TokenSource(Protocol):
    """`BaseApi` 需要的令牌来源接口。

    声成一个 Protocol 而不是直接标注成 `TokenProvider`：
    `BaseApi` 是通用传输层，不该依赖 MES 那一层的实现类，
    否则以后别的后端想接自己的令牌来源就得先继承 MES 的类。
    这也让"用假 provider 测 401 重放"成为可能（见 test_token_lifecycle.py）。
    """

    @property
    def token(self) -> str: ...

    def refresh_after(self, stale_token: str) -> str: ...


@dataclass(frozen=True)
class LoginResult:
    """一次登录的产物。`expires_in` 直接用服务端返回的 ttl，不自己猜。"""

    token: str
    expires_in: int
    user: dict


class TokenProvider:
    """某个账号的令牌状态机：`token` 属性永远返回当前可用的令牌。

    `fetch` 是"怎么登录"这一步，由调用方注入（MesApi 传自己的登录实现），
    这样 provider 不需要知道 HTTP 细节，测试也能塞一个计数用的假登录进去。
    """

    def __init__(
        self,
        fetch: Callable[[], LoginResult],
        *,
        refresh_margin: int | None = None,
        name: str = "token",
    ):
        self._fetch = fetch
        self.name = name
        self.refresh_margin = (
            refresh_margin
            if refresh_margin is not None
            else int(os.getenv(REFRESH_MARGIN_ENV, str(DEFAULT_REFRESH_MARGIN_SECONDS)))
        )
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        # 最近一次登录返回的用户信息。`as_role()` 要用它填 RoleClient 的
        # username/role（以服务端返回的为准，而不是拿本地表里的角色名猜）。
        self.user: dict | None = None
        # 每次真正登录后 +1。并发的"只刷一次"靠它自证：不是靠注释说锁住了。
        self._generation = 0
        self.login_count = 0

    # ---------- 读 ----------
    @property
    def token(self) -> str:
        """当前可用令牌；没有或将要过期时先刷新。"""
        # 快路径不加锁：绝大多数请求走到这里时令牌都是新鲜的，
        # 给每次请求都加一把全局锁会把并发用例本身压成串行。
        # `seen` 必须在**进锁前**读：它记的是"我判断需要刷新时看到的版本"，
        # 进了锁再读就只能读到已经被别人改过的版本，那道确认形同虚设。
        seen = self._generation
        cached = self._token
        if cached is not None and not self._needs_refresh():
            return cached

        with self._lock:
            if self._generation != seen:
                # 等锁期间别人已经刷好了，直接用他的结果 —— 这才叫"只刷一次"。
                # 少了这道确认，每个排队的线程都会各自登录一遍。
                return self._require_token()
            if self._needs_refresh():
                self._refresh_locked()
            return self._require_token()

    @property
    def expires_at(self) -> float:
        """到期时刻（epoch 秒）；从未登录时为 0.0。"""
        return self._expires_at

    def is_expiring(self) -> bool:
        """是否已进入提前刷新窗口（令牌缺失也算）。"""
        return self._needs_refresh()

    def _needs_refresh(self) -> bool:
        if self._token is None:
            return True
        return time.time() >= self._expires_at - self.refresh_margin

    def _require_token(self) -> str:
        if self._token is None:  # pragma: no cover - _refresh_locked 成功后不可能为 None
            raise RuntimeError(f"令牌刷新后仍为空: {self.name}")
        return self._token

    # ---------- 写 ----------
    def refresh_after(self, stale_token: str) -> str:
        """因为 `stale_token` 被服务端拒了而换令牌；若别人已经换过就直接复用。

        `stale_token` 是本次请求实际用的那个令牌。进锁后先比对它：
        如果当前令牌已经不等于它，说明在我等锁期间别的线程已经换好了 ——
        这时再登录一次纯属浪费，而且正是"并发没去重"的表现形态。

        为什么不用 `refresh()` 的无条件登录：一批并发请求可能同时吃到
        **同一个**过期令牌的 401，无条件登录会让每个线程各登录一次；
        令牌刷新最终是能成功的，所以这种缺陷在功能测试里完全看不出来，
        只有数登录次数才能发现。契约测试的价值就在这。

        用在"服务端已经判 401"的场景：此时不能信本地时钟
        （`is_expiring()` 可能还是 False），必须真去登录一次。
        """
        with self._lock:
            if self._token is not None and self._token != stale_token:
                return self._token
            self._refresh_locked()
            return self._require_token()

    def refresh(self) -> str:
        """无条件换一个新令牌并返回它（用于人工/测试强制刷新）。"""
        with self._lock:
            self._refresh_locked()
            return self._require_token()

    def invalidate(self) -> None:
        """丢弃当前令牌，下次取值时重新登录。"""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _refresh_locked(self) -> None:
        result = self._fetch()
        self.login_count += 1
        self._token = result.token
        self.user = result.user
        # 用服务端给的 ttl 算到期时刻，而不是写死一个本地常量：
        # 两端 TTL 不一致时，以服务端为准才不会出现"客户端以为还有效"。
        self._expires_at = time.time() + result.expires_in
        self._generation += 1
