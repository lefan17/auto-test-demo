import os

import pytest

from common.assert_util import assert_status_code

pytestmark = pytest.mark.api


def test_response_time_should_be_acceptable(user_api):
    """响应时间基线：接口变慢要能被发现。

    阈值通过环境变量传入，本地严、CI 松，避免流水线因网络抖动误报。
    用法: $env:MAX_RESPONSE_MS=500; pytest -m api

    注意两点（都是踩过的）：
    1. `elapsed_ms` 只计「产出响应的那一次请求」，不含之前失败重试的等待，
       所以一次成功的重试不会因为把失败那次的耗时算进来而误报。
    2. 跑公网接口时这条路测的是"你家到 jsonplaceholder 的速度"，不是被测系统。
       实测国内直连海外常在 2-4 秒波动，所以默认阈值放到 5 秒；
       换成内网接口后，可以收紧到 200-500ms 才是真正有意义的性能门禁。
    """
    max_ms = int(os.getenv("MAX_RESPONSE_MS", "5000"))

    resp = user_api.get_users()

    assert_status_code(resp, 200)
    assert resp.elapsed_ms < max_ms, (
        f"接口响应过慢: {resp.elapsed_ms:.0f}ms >= 阈值 {max_ms}ms\n"
        f"（本地网络波动也会触发，阈值用环境变量 MAX_RESPONSE_MS 调整）"
    )


def test_request_should_send_json_content_type(user_api):
    """请求头校验：POST 必须带 Content-Type: application/json。"""
    resp = user_api.create_user({"name": "header check"})

    content_type = resp.request.headers.get("Content-Type", "")
    assert "application/json" in content_type, f"Content-Type 不正确: {content_type}"
