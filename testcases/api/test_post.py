import pytest

from common.assert_util import assert_field_equals, assert_json_schema, assert_status_code
from common.data_util import load_json
from data.schemas import POST_SCHEMA

pytestmark = [pytest.mark.api, pytest.mark.regression]

# 用例开始就加载数据文件：数据驱动，用例逻辑不含具体数据
USER_DATA = load_json("users.json")


def test_create_post_should_echo_payload(post_api):
    """创建文章：服务端应回显提交的字段。"""
    payload = {"userId": 1, "title": "自动化测试入门", "body": "一周计划"}

    resp = post_api.create_post(payload)

    assert_status_code(resp, 201)
    assert_json_schema(resp, POST_SCHEMA, name="创建文章响应")
    body = resp.json()
    assert_field_equals(body["title"], payload["title"], "title")
    assert_field_equals(body["userId"], payload["userId"], "userId")


@pytest.mark.parametrize(
    "user_key",
    ["valid_user", "edge_user"],
    ids=["valid-user", "edge-user"],
)
def test_post_with_data_from_file(post_api, user_key):
    """从 data/users.json 读取数据驱动用例 —— 加数据不改代码。"""
    user = USER_DATA[user_key]
    payload = {"userId": 1, "title": f"{user['name']} 的标题", "body": user["username"]}

    resp = post_api.create_post(payload)

    assert_status_code(resp, 201)
    assert_field_equals(resp.json()["title"], payload["title"], "title")


def test_filter_posts_by_user_id(post_api):
    """关联查询：按 userId 过滤，返回结果必须都属于该用户。"""
    resp = post_api.get_posts(userId=2)

    assert_status_code(resp, 200)
    posts = resp.json()
    assert posts, "过滤结果不应为空"
    assert all(p["userId"] == 2 for p in posts), f"存在不属于 userId=2 的数据: {posts[:2]}"


@pytest.mark.parametrize("limit", [5, 10, 20], ids=lambda v: f"limit-{v}")
def test_posts_pagination_limit(post_api, limit):
    """分页参数：_limit 应生效，返回条数不超过限制。"""
    resp = post_api.get_posts(_limit=limit)

    assert_status_code(resp, 200)
    assert len(resp.json()) <= limit, f"_limit={limit} 未生效，实际返回 {len(resp.json())} 条"


def test_update_post_should_reflect_change(post_api):
    """更新接口：改完再查应能读到新值（真实项目里要用数据库二次确认）。"""
    resp = post_api.put("/posts/1", json={"id": 1, "title": "updated", "userId": 1, "body": "x"})

    assert_status_code(resp, 200)
    assert_field_equals(resp.json()["title"], "updated", "title")
