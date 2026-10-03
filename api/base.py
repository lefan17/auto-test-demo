"""接口封装层：所有请求都从这里走，用例里不出现裸 requests。

为什么这么设计（面试常问）：
1. 统一加超时，避免一条用例把整个流水线卡死；
2. 统一日志，失败时能回溯请求和耗时；
3. 换域名/换鉴权方式只改这一处，用例不动。
"""
import logging
import os
import time

import allure
import requests

logger = logging.getLogger(__name__)

# 需要重试的响应码：429 是被限流（公共测试接口常见），5xx 是服务端抖动
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# 只有「幂等」请求才允许重试。POST 不在其中——重放创建请求可能造出重复数据，
# 这类副作用比一次失败更麻烦。创建类接口要重试的话，得靠业务上的幂等键。
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class BaseApi:
    def __init__(self, base_url: str, timeout: int = None, retries: int = None):
        self.base_url = base_url.rstrip("/")
        # 环境和用例的优先级：显式传参 > 环境变量 > 默认值。
        # 让别人不改进代码就能调：MAX_RESPONSE_MS 管断言阈值，这两个管传输层。
        self.timeout = timeout or int(os.getenv("API_TIMEOUT", "15"))
        self.retries = retries if retries is not None else int(os.getenv("API_RETRIES", "2"))
        self.session = requests.Session()
        # 会话级默认请求头；每条请求可通过 headers= 覆盖
        self.session.headers.update({"Content-Type": "application/json"})
        # 最近一次响应，调试时可用
        self.last_response = None

    # ---------- 核心请求方法 ----------
    def _should_retry(self, method: str, response) -> bool:
        """是否该重试：幂等方法 + 命中需要重试的状态码。"""
        method = method.upper()
        if method in IDEMPOTENT_METHODS:
            return response.status_code in RETRY_STATUS
        # POST：只在被限流时重试。限流意味着请求根本没被处理，重放是安全的；
        # 而 5xx 时服务端可能已经落库了，重放会造重复数据。
        return method == "POST" and response.status_code == 429

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        method = method.upper()

        last_error = None
        retried = False  # 本次请求是否经过了重试（写进报告，便于区分"慢"和"抖"）
        for attempt in range(self.retries + 1):
            # 传输层失败后的重试，绕过系统代理再试一次。
            # 为什么：本机开着 Clash/v2ray 这类代理时，requests 会自动走它，
            # 而代理对某些域名会直接掐断 TLS（报 UNEXPECTED_EOF_WHILE_READING）。
            # 直连往往反而是通的。手动把代理从这次请求里摘掉，比让人去猜
            # "是不是我用例写错了"友好得多。
            req_kwargs = kwargs
            if attempt > 0 and last_error is not None and os.getenv("API_TRUST_ENV", "1") == "1":
                req_kwargs = {**kwargs, "proxies": {"http": None, "https": None}}

            start = time.perf_counter()
            logger.info("--> %s %s params=%s (第 %d 次%s)",
                        method, url, kwargs.get("params"), attempt + 1,
                        "，已绕过代理" if req_kwargs is not kwargs else "")
            try:
                response = self.session.request(method, url, **req_kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # 连接失败、读超时、SSL 握手失败都是「传输没成」，
                # 请求大概率没被处理，重试是安全的。requests.SSLError 也继承自
                # ConnectionError，所以代理掐 TLS 的情况会走到这里。
                last_error = exc
                logger.warning("<-- %s %s 传输失败(%d/%d): %s",
                               method, url, attempt + 1, self.retries + 1, exc)
                if attempt < self.retries:
                    time.sleep(0.5 * (2**attempt))  # 退避 0.5s、1s
                    retried = True
                    continue
                raise

            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("<-- %s %s [%s] %.0fms", method, url, response.status_code, elapsed_ms)
            if not response.ok:
                logger.warning("<-- 失败响应: %s", response.text[:500])

            if self._should_retry(method, response) and attempt < self.retries:
                logger.warning("<-- %s %s [%s] 命中重试条件(%d/%d)",
                               method, url, response.status_code, attempt + 1, self.retries + 1)
                time.sleep(0.5 * (2**attempt))
                retried = True
                continue

            self.last_response = response
            # 只统计「真正产出这个响应」的那一次请求的耗时。
            # 不能把前面失败/重试的几次累加进去：否则一次成功的重试会因为
            # 把上一次的等待也算进来而误报超时——恢复成功反而判失败，很荒唐。
            response.elapsed_ms = elapsed_ms
            response.was_retried = retried
            # 打进 Allure 报告：测试报告里能看到每个请求和耗时
            with allure.step(f"{method} {url} -> {response.status_code} ({elapsed_ms:.0f}ms)"):
                allure.attach(
                    f"URL: {url}\nStatus: {response.status_code}\nTime: {elapsed_ms:.0f}ms\n"
                    f"Retried: {'是（此耗时仅计最后一次请求）' if retried else '否'}\n\n"
                    f"{response.text[:2000]}",
                    name="HTTP 请求详情",
                    attachment_type=allure.attachment_type.TEXT,
                )
            return response

        # 理论到不了这里（循环内要么 return 要么 raise），留着以防以后改坏
        raise last_error if last_error else RuntimeError(f"{method} {url} 重试耗尽")

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
        """统一设置鉴权头；真实项目里 token 通常由登录接口返回后注入。"""
        self.session.headers[header_name] = f"{prefix}{token}" if prefix else token
