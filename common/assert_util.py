"""统一断言封装：让失败信息能直接看出「哪个接口、期望什么、实际什么」。

pytest 的断言失败信息本身够用，但接口断言多了一层上下文（URL、响应体），
封装一层能省掉大量「接口挂了还要自己去翻日志」的时间。
"""
import json
from typing import Any

import allure
from jsonschema import ValidationError, validate


def assert_status_code(response, expected: int = 200):
    """校验 HTTP 状态码。失败时把 URL 和响应前 500 字符打出来。"""
    body = response.text[:500] if response.text else "<empty>"
    assert response.status_code == expected, (
        f"\nURL      : {response.request.method} {response.url}\n"
        f"期望状态码: {expected}\n"
        f"实际状态码: {response.status_code}\n"
        f"响应内容  : {body}"
    )


def assert_json_schema(response, schema: dict, name: str = ""):
    """用 jsonschema 校验响应结构（字段名、类型、必填）。

    这是接口测试里最值钱的一条断言：状态码对了不代表字段没被改坏。
    """
    try:
        data = response.json()
    except json.JSONDecodeError as e:
        raise AssertionError(f"响应不是合法 JSON: {e}\n原始内容: {response.text[:300]}")

    with allure.step(f"校验 JSON Schema: {name or 'response'}"):
        try:
            validate(instance=data, schema=schema)
        except ValidationError as e:
            path = " -> ".join(str(p) for p in e.absolute_path) or "<root>"
            raise AssertionError(
                f"\nSchema 校验失败: {name}\n"
                f"字段路径: {path}\n"
                f"原因    : {e.message}\n"
                f"实际值  : {e.instance}"
            )


def assert_field_equals(actual: Any, expected: Any, field: str = ""):
    """业务字段断言，失败信息带上字段名。"""
    assert actual == expected, f"字段 [{field}] 不匹配: 期望 {expected!r}, 实际 {actual!r}"
