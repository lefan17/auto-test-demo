import os

import pytest

from common.assert_util import assert_status_code

pytestmark = pytest.mark.api


def test_response_time_should_be_acceptable(user_api):
    """响应时间基线：接口变慢要能被发现。

    阈值通过环境变量传入，本地严、CI 松，避免流水线因网络抖动误报。
    用法: $env:MAX_RESPONSE_MS=500; pytest -m api
    """
    max_ms = int(os.getenv("MAX_RESPONSE_MS", "3000"))

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
