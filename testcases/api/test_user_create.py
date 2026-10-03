import pytest

from common.assert_util import assert_field_equals, assert_json_schema, assert_status_code
from data.schemas import USER_CREATED_SCHEMA, USER_LIST_SCHEMA, USER_SCHEMA

pytestmark = [pytest.mark.api, pytest.mark.smoke]


@pytest.mark.parametrize(
    "case_name, expected_word",
    [
        ("常规用户", "Leanne"),
        ("中文用户名", "张三"),
        ("超长用户名", "a" * 200),
    ],
    ids=["normal", "chinese", "long-name"],
)
def test_create_user_with_various_names(user_api, case_name, expected_word):
    """参数化 + 数据驱动：同一段逻辑跑多组数据，用例数量翻倍而代码不重复。"""
    payload = {"name": case_name, "username": "qa_01", "email": "qa_01@example.com"}

    resp = user_api.create_user(payload)

    assert_status_code(resp, 201)
    assert_json_schema(resp, USER_CREATED_SCHEMA, name="创建用户响应")
    assert_field_equals(resp.json()["name"], case_name, "name")


def test_create_user_returns_generated_id(user_api):
    """创建后服务端应返回自增 id，且是整数。"""
    resp = user_api.create_user({"name": "new user", "username": "newbie"})

    assert_status_code(resp, 201)
    new_id = resp.json()["id"]
    assert isinstance(new_id, int), f"id 类型应为 int，实际 {type(new_id)}: {new_id!r}"


def test_get_user_detail_should_match_contract(user_api):
    """详情接口结构契约：字段名和类型都不能被改坏。"""
    resp = user_api.get_user(1)

    assert_status_code(resp, 200)
    assert_json_schema(resp, USER_SCHEMA, name="用户详情")
    assert_field_equals(resp.json()["id"], 1, "id")


@pytest.mark.parametrize("user_id", [1, 2, 5, 10], ids=lambda v: f"user-{v}")
def test_get_different_users(user_api, user_id):
    """参数化覆盖多条数据，避免只测 id=1 的「假通过」。"""
    resp = user_api.get_user(user_id)

    assert_status_code(resp, 200)
    assert_json_schema(resp, USER_SCHEMA, name=f"用户{user_id}详情")
    assert_field_equals(resp.json()["id"], user_id, "id")


def test_email_field_should_be_valid_format(user_api):
    """业务校验：email 字段必须符合邮箱格式（爬虫类接口常在这里出问题）。"""
    users = user_api.get_users().json()

    bad = [u["email"] for u in users if "@" not in u.get("email", "")]
    assert not bad, f"存在非法邮箱格式的数据: {bad}"


def test_user_ids_should_be_unique(user_api):
    """数据一致性：列表里的 id 不能重复。"""
    ids = [u["id"] for u in user_api.get_users().json()]

    assert len(ids) == len(set(ids)), f"用户 id 存在重复: {ids}"
