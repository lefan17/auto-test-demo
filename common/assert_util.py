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
        # 链上原始异常：否则 pytest 只会显示最后一句，
        # JSON 解析失败的字符位置（问题最关键的线索）就被吞掉了。
        raise AssertionError(f"响应不是合法 JSON: {e}\n原始内容: {response.text[:300]}") from e

    with allure.step(f"校验 JSON Schema: {name or 'response'}"):
        try:
            validate(instance=data, schema=schema)
        except ValidationError as e:
            path = " -> ".join(str(p) for p in e.absolute_path) or "<root>"
            raise AssertionError(
                f"\nSchema 校验失败: {name}\n字段路径: {path}\n原因    : {e.message}\n实际值  : {e.instance}"
            ) from e
        except Exception as e:
            # 兜底：schema 本身坏掉的情况（$ref 解析不到、关键字拼错等）。
            # jsonschema 在这类情况下抛的异常类型并不稳定（_RefResolutionError
            # 还是个私有类），而它们的共同点是"根本不是被测接口的问题"。
            # 不兜住的话，用例会以 ERROR 而不是 FAILED 的形式冒出来，
            # 看报告的人会以为"环境炸了"，实际只是 data/schemas.py 里打错了一个字。
            raise AssertionError(
                f"\nJSON Schema 本身有问题（请检查 data/schemas.py）: {name}\n异常类型: {type(e).__name__}\n原因: {e}"
            ) from e


def assert_field_equals(actual: Any, expected: Any, field: str = ""):
    """业务字段断言，失败信息带上字段名。"""
    assert actual == expected, f"字段 [{field}] 不匹配: 期望 {expected!r}, 实际 {actual!r}"
