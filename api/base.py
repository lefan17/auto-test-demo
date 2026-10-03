"""接口封装层：所有请求都从这里走，用例里不出现裸 requests。

为什么这么设计（面试常问）：
1. 统一加超时，避免一条用例把整个流水线卡死；
2. 统一日志，失败时能回溯请求和耗时；
3. 换域名/换鉴权方式只改这一处，用例不动。
"""
import logging
import time

import allure
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class BaseApi:
    def __init__(self, base_url: str, timeout: int = 10, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        # 会话级默认请求头；每条请求可通过 headers= 覆盖
        self.session.headers.update({"Content-Type": "application/json"})
        # 重试策略：只对「连接失败」和「5xx」重试。
        # 为什么必须加重试：公网接口/测试环境抖动是常态，没有重试的自动化
        # 会产生大量「假失败」，团队很快就会不再信任这套用例。
        retry = Retry(
            total=retries,
            backoff_factor=0.5,  # 退避：0.5s、1s...
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        # 最近一次响应，调试时可用
        self.last_response = None

    # ---------- 核心请求方法 ----------
    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)

        start = time.perf_counter()
        logger.info("--> %s %s params=%s", method.upper(), url, kwargs.get("params"))
        response = self.session.request(method, url, **kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000

        logger.info("<-- %s %s [%s] %.0fms", method.upper(), url, response.status_code, elapsed_ms)
        if not response.ok:
            logger.warning("<-- 失败响应: %s", response.text[:500])

        self.last_response = response
        # 挂到响应对象上，用例里可以直接断言响应时间
        response.elapsed_ms = elapsed_ms
        # 打进 Allure 报告：测试报告里能看到每个请求和耗时
        with allure.step(f"{method.upper()} {url} -> {response.status_code} ({elapsed_ms:.0f}ms)"):
            allure.attach(
                f"URL: {url}\nStatus: {response.status_code}\nTime: {elapsed_ms:.0f}ms\n\n"
                f"{response.text[:2000]}",
                name="HTTP 请求详情",
                attachment_type=allure.attachment_type.TEXT,
            )
        return response

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
