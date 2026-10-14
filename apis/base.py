"""接口封装层：所有请求都从这里走，用例里不出现裸 requests。

为什么这么设计（面试常问）：
1. 统一加超时，避免一条用例把整个流水线卡死；
2. 统一日志，失败时能回溯请求和耗时；
3. 换域名/换鉴权方式只改这一处，用例不动。
"""

import logging
import math
import os
import random
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import allure
import requests

if TYPE_CHECKING:  # 只为类型标注；运行期不 import，避免 apis 包内的循环依赖
    from apis.token_provider import TokenSource

logger = logging.getLogger(__name__)

# 需要重试的响应码：429 是被限流（公共测试接口常见），5xx 是服务端抖动
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# 只有「幂等」请求才允许重试。POST 不在其中——重放创建请求可能造出重复数据，
# 这类副作用比一次失败更麻烦。创建类接口要重试的话，得靠业务上的幂等键。
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# 「401 → 换令牌 → 重放一次」额外占用的尝试次数。
# 为什么不能复用 self.retries 的额度：`MesApi` 的默认 retries 是 0（用例要精确
# 数请求次数），如果重放也从这个额度里扣，默认配置下重放就永远不会发生——
# 加了功能却默认不生效，是最难发现的那种坏。
REAUTH_EXTRA_ATTEMPTS = 1

# 指数退避的基数：第 attempt 次重试等 RETRY_BACKOFF_BASE_SECONDS * 2**attempt 秒。
RETRY_BACKOFF_BASE_SECONDS = 0.5

# 退避抖动的上界比例：实际等待落在 [base, base * (1 + ratio)] 这个闭区间里。
# 为什么要抖动：并发用例会在同一刻撞上 429，没有抖动时它们会在同一刻醒来、
# 再一起撞上去，限流窗口反而被持续打满——「退避」变成了「同步重试」。
# 为什么只加不减（而不是 base * uniform(1-ratio, 1+ratio)）：等待下界不能低于
# 改造前的 0.5s、1s，否则这次改动会悄悄变成对服务端加压。
RETRY_BACKOFF_JITTER_RATIO = 0.25

# 单次重试等待的硬上限。没有上限时，一个回 `Retry-After: 3600` 的限流响应
# 会把整条用例挂住一小时——服务端给的等待时长是替它自己算的，不是替「跑一次
# 回归」算的。默认取 30s 的理由：单次请求的 timeout 默认 15s，等待超过两个
# timeout 之后，对端要么已经恢复、要么这条用例本来就该红，再等只是把失败往后拖。
# 可用环境变量 API_MAX_RETRY_WAIT（秒）覆盖，给「服务端确实需要长窗口」的场景留口子。
DEFAULT_MAX_RETRY_WAIT = 30.0


class BaseApi:
    def __init__(self, base_url: str, timeout: int = None, retries: int = None, max_retry_wait: float | None = None):
        self.base_url = base_url.rstrip("/")
        # 环境和用例的优先级：显式传参 > 环境变量 > 默认值。
        # 让别人不改进代码就能调：MAX_RESPONSE_MS 管断言阈值，这两个管传输层。
        self.timeout = timeout or int(os.getenv("API_TIMEOUT", "15"))
        self.retries = retries if retries is not None else int(os.getenv("API_RETRIES", "2"))
        self.max_retry_wait = (
            max_retry_wait
            if max_retry_wait is not None
            else float(os.getenv("API_MAX_RETRY_WAIT", DEFAULT_MAX_RETRY_WAIT))
        )
        # 等待、时钟与随机数都做成实例属性，而不是在调用点直接写 time.sleep()：
        # 测试要断言「等了多久」，而真的等下去会让一条用例从毫秒变成秒级。
        # 为什么不用 monkeypatch 打补丁 time.sleep 这个全局：那是进程级的，
        # 一旦有测试忘了还原，"生产行为被静默改短"就成了一件没人能发现的事。
        self._sleep: Callable[[float], None] = time.sleep
        self._rng = random.Random()
        self.session = requests.Session()
        # 会话级默认请求头；每条请求可通过 headers= 覆盖
        self.session.headers.update({"Content-Type": "application/json"})
        # 最近一次响应，调试时可用
        self.last_response = None
        # 鉴权头的位置与前缀。抽成属性是因为不同网关约定不同
        # （有的用 `X-Auth-Token`、有的不加 `Bearer `），换网关时不该改请求逻辑。
        self.token_header = "Authorization"
        self.token_prefix = "Bearer "

    # ---------- 核心请求方法 ----------
    def _should_retry(self, method: str, response) -> bool:
        """是否该重试：幂等方法 + 命中需要重试的状态码。"""
        method = method.upper()
        if method in IDEMPOTENT_METHODS:
            return response.status_code in RETRY_STATUS
        # POST：只在被限流时重试。限流意味着请求根本没被处理，重放是安全的；
        # 而 5xx 时服务端可能已经落库了，重放会造重复数据。
        return method == "POST" and response.status_code == 429

    def _retry_after_seconds(self, response) -> float | None:
        """读 `Retry-After` 头并换算成秒；头缺失或读不懂时返回 None（交由退避兜底）。

        两种格式都要支持（RFC 9110 §10.2.3）：delta-seconds（`Retry-After: 2`）
        与 HTTP-date（`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`）。
        返回值 0.0 是合法的，意思是"服务端说现在就能重试"（见下面的日期分支）。

        为什么用 `parsedate_to_datetime` 而不是自己写格式化：HTTP-date 有
        IMF-fixdate / RFC 850 / asctime 三种历史格式，`email.utils` 三种都认，
        自己写只会漏掉后两种——而漏掉的后果是"默默退回退避"，没人会注意到。
        """
        raw = response.headers.get("Retry-After") if response.headers else None
        # 非字符串一律当作没有这个头：headers 可能来自测试桩或被中间层改写，
        # 让一个类型不对的值把整条请求炸掉，比退避重试糟糕得多。
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        if not value:
            return None
        is_date = False
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            # 不是数字，那就按 HTTP-date 试一次。两个都读不懂才算非法。
            try:
                when = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if when is None:
                return None
            is_date = True
            # 没有时区的日期按 UTC 解释：HTTP-date 本来就该带 GMT，
            # 但服务端漏了后缀时若按本地时区算，会凭空多出 8 小时等待（本机是 UTC+8）。
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        # NaN/Inf 会让后面所有的比较都变成 False，一路穿过 cap 检查，最后变成
        # `sleep(nan)`；这种头读不懂，退回退避。
        if not math.isfinite(seconds):
            return None
        if seconds <= 0:
            # 两种"非正数"性质不同，所以处理也不同：
            # 负的 delta-seconds（如 `-1`）是服务端给了个不合规的数值，我们读不懂它，
            # 退回自己的退避更安全；而过去时刻的 HTTP-date 是标准允许的表述
            # ——"不早于这一刻重试"，既然已经过了，就是立刻可以重试，返回 0.0。
            return 0.0 if is_date else None
        return seconds

    def _retry_wait(self, attempt: int, response=None) -> float:
        """算出这次重试该等多久：优先服务器给的 `Retry-After`，否则退避 + 抖动。

        传输失败时 `response` 是 None，没有头可读，走退避。
        """
        wait = self._retry_after_seconds(response) if response is not None else None
        if wait is None:
            wait = RETRY_BACKOFF_BASE_SECONDS * (2**attempt)
            # 抖动只往上加，且区间有界：这样「等多久」这件事是可断言的
            # （见 tools/check_retry.py 的闭区间用例），而不是只能靠猜。
            wait *= 1 + self._rng.uniform(0, RETRY_BACKOFF_JITTER_RATIO)
        # 上限对两条路径都生效：`Retry-After: 3600` 和 attempt 涨上去后的退避
        # 都会撞上它。截断而不是抛异常——被限流时多等一会儿仍然比直接失败好。
        return min(wait, self.max_retry_wait)

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        method = method.upper()
        # 令牌来源（可选）：由 RoleClient 注入。见本文件末尾 `_reauth` 的说明。
        token_source: TokenSource | None = kwargs.pop("_token_source", None)

        last_error = None
        retried = False  # 本次请求是否经过了重试（写进报告，便于区分"慢"和"抖"）
        reauthed = False  # 本次请求是否经过"401 → 换令牌 → 重放"
        # 本次请求实际用掉的令牌。401 之后要把它交给 provider：
        # 只有 provider 知道"这个令牌是不是已经被别人换掉了"，
        # 无条件刷新会让并发吃到同一个过期令牌的请求各登录一次。
        used_token: str | None = None
        # 上限里额外留出 REAUTH_EXTRA_ATTEMPTS 次：重放不该和"传输/5xx 重试"
        # 抢同一份额度，两者的触发条件不同、要防的东西也不同。
        for attempt in range(self.retries + 1 + REAUTH_EXTRA_ATTEMPTS):
            # 传输层失败后的重试，绕过系统代理再试一次。
            # 为什么：本机开着 Clash/v2ray 这类代理时，requests 会自动走它，
            # 而代理对某些域名会直接掐断 TLS（报 UNEXPECTED_EOF_WHILE_READING）。
            # 直连往往反而是通的。手动把代理从这次请求里摘掉，比让人去猜
            # "是不是我用例写错了"友好得多。
            req_kwargs = kwargs
            bypassed_proxy = False
            if attempt > 0 and last_error is not None and os.getenv("API_TRUST_ENV", "1") == "1":
                req_kwargs = {**kwargs, "proxies": {"http": None, "https": None}}
                bypassed_proxy = True
            # 每次尝试都重新取令牌，而不是在进入循环前取一次。
            # 为什么：重放的意义就是"用换过的新令牌再发一次"，如果头部在循环外
            # 就固化成字符串，刷新过的令牌根本到不了这一层，重放只会再吃一个 401。
            used_token = None
            if token_source is not None:
                req_kwargs, used_token = self._with_token(req_kwargs, token_source)

            start = time.perf_counter()
            logger.info(
                "--> %s %s params=%s (第 %d 次%s)",
                method,
                url,
                kwargs.get("params"),
                attempt + 1,
                "，已绕过代理" if bypassed_proxy else "",
            )
            try:
                response = self.session.request(method, url, **req_kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # 连接失败、读超时、SSL 握手失败都是「传输没成」，
                # 请求大概率没被处理，重试是安全的。requests.SSLError 也继承自
                # ConnectionError，所以代理掐 TLS 的情况会走到这里。
                last_error = exc
                logger.warning("<-- %s %s 传输失败(%d/%d): %s", method, url, attempt + 1, self.retries + 1, exc)
                if attempt < self.retries:
                    # 传输失败时没有响应头，只能走退避（基数 0.5s，抖动，带上限）
                    self._sleep(self._retry_wait(attempt))
                    retried = True
                    continue
                raise

            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("<-- %s %s [%s] %.0fms", method, url, response.status_code, elapsed_ms)
            if not response.ok:
                logger.warning("<-- 失败响应: %s", response.text[:500])

            if token_source is not None and self._should_reauth(method, response, used_token, reauthed):
                # 到这里说明令牌在服务端已经失效，但本地还没到期（或刚刚到期）。
                # 此时必须**强制**登录一次，不能只看 `is_expiring()`：
                # 服务端 TTL 比本地短、令牌被吊销、时钟偏移都会让本地判断为"还有效"。
                reauthed = True
                retried = True
                logger.warning("<-- %s %s [401] 令牌已失效，换新令牌后重放一次", method, url)
                # 把**本次实际用的令牌**交给 provider，而不是无条件刷新：
                # 并发吃到同一个过期令牌时，第一个换成功后其余线程会直接复用，
                # 于是 N 个 401 只产生 1 次登录。
                token_source.refresh_after(used_token or "")
                continue

            if self._should_retry(method, response) and attempt < self.retries:
                logger.warning(
                    "<-- %s %s [%s] 命中重试条件(%d/%d)",
                    method,
                    url,
                    response.status_code,
                    attempt + 1,
                    self.retries + 1,
                )
                # 这里的 response 可能就是 429：服务端给的 `Retry-After` 优先于
                # 我们自己算的退避，它比我们更清楚窗口什么时候开。
                self._sleep(self._retry_wait(attempt, response))
                retried = True
                continue

            self.last_response = response
            # 只统计「真正产出这个响应」的那一次请求的耗时。
            # 不能把前面失败/重试的几次累加进去：否则一次成功的重试会因为
            # 把上一次的等待也算进来而误报超时——恢复成功反而判失败，很荒唐。
            # 为什么用 response.__dict__ 而不是 `response.elapsed_ms = ...`：
            # requests 2.34 起自带 py.typed，mypy 开始真的检查 requests.Response，
            # 直接挂属性会报 [attr-defined]（属性确实不存在，报得没错）。
            # 也不用 setattr(response, "elapsed_ms", ...)：ruff 的 B010 会拦
            # （常量属性名用 setattr 并不比直接赋值安全）。写进实例字典是唯一
            # 同时被 mypy 与 ruff 接受、且不动调用方的写法——这几个属性是
            # testcases/api/test_response_time.py、tools/check_retry.py 与
            # testcases/mes/test_token_lifecycle.py 依赖的公开接口。
            response.__dict__["elapsed_ms"] = elapsed_ms
            response.__dict__["was_retried"] = retried
            response.__dict__["was_reauthed"] = reauthed
            # 打进 Allure 报告：测试报告里能看到每个请求和耗时
            with allure.step(f"{method} {url} -> {response.status_code} ({elapsed_ms:.0f}ms)"):
                allure.attach(
                    f"URL: {url}\nStatus: {response.status_code}\nTime: {elapsed_ms:.0f}ms\n"
                    f"Retried: {'是（此耗时仅计最后一次请求）' if retried else '否'}\n"
                    f"Reauthenticated: {'是（此响应来自换令牌后的重放）' if reauthed else '否'}\n\n"
                    f"{response.text[:2000]}",
                    name="HTTP 请求详情",
                    attachment_type=allure.attachment_type.TEXT,
                )
            return response

        # 理论到不了这里（循环内要么 return 要么 raise），留着以防以后改坏
        raise last_error if last_error else RuntimeError(f"{method} {url} 重试耗尽")

    def _should_reauth(self, method: str, response, used_token: str | None, reauthed: bool) -> bool:
        """是否该「换令牌 + 重放一次」。

        四个条件是缺一不可的：
        1. 确实拿到了 401（而不是 403）——403 是权限不足，换令牌解决不了，
           重放只会把同一个 403 再要一遍，白白多打一次服务端；
        2. 方法在 `IDEMPOTENT_METHODS` 里——这是本文件早就定好的边界，
           POST 不在其中：一个"创建工单"的请求被重放，可能造出第二条工单，
           而它的失败原因可能只是"令牌恰好在处理过程中过期"，
           服务端其实已经落库了。宁可让这条用例红，也不要静默造重复数据；
        3. 这一轮还没有重放过——重放必须最多一次，否则令牌持续失效时会变成
           "401 → 刷新 → 401 → 刷新"的死循环；
        4. 这次用的确实是 provider 给的令牌（`used_token is not None`）。
           调用方自己传了 Authorization 时（例如篡改令牌的用例），
           "换令牌重放"没有意义：请求根本没走 provider 的令牌，
           刷新之后用的还是调用方那个头，重放只会再吃一个 401。
        """
        return response.status_code == 401 and used_token is not None and not reauthed and method in IDEMPOTENT_METHODS

    def _with_token(self, req_kwargs: dict, token_source) -> tuple[dict, str | None]:
        """把当前令牌塞进本次请求的头部；调用方显式传的 headers 优先。

        返回 `(新的请求参数, 本次实际使用的令牌)`。第二个返回值有两种含义，
        调用方（`request()` 的 401 分支）要靠它区分"这次用的是 provider 的令牌"
        和"这次用的是调用方自己传的 Authorization"：后者不该在 401 之后
        拿 provider 的令牌去比对，否则会误判成"别人已经换过"而跳过刷新。
        """
        headers = dict(req_kwargs.get("headers") or {})
        if self.token_header in headers:
            return req_kwargs, None
        token = token_source.token
        headers[self.token_header] = f"{self.token_prefix}{token}" if self.token_prefix else token
        return {**req_kwargs, "headers": headers}, token

    # ---------- 语法糖 ----------
    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    def set_token(self, token: str, header_name: str = "Authorization", prefix: str = "Bearer "):
        """统一设置鉴权头；真实项目里 token 通常由登录接口返回后注入。

        同时记住头名与前缀，供 `_with_token()` 复用 —— 否则"换了个头名"
        这件事只在手工设令牌时生效，走 provider 的请求又会退回默认头名。
        """
        self.token_header = header_name
        self.token_prefix = prefix
        self.session.headers[header_name] = f"{prefix}{token}" if prefix else token
