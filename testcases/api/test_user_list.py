import pytest

from common.assert_util import assert_field_equals, assert_json_schema, assert_status_code
from data.schemas import USER_LIST_SCHEMA, USER_SCHEMA

pytestmark = [pytest.mark.api, pytest.mark.smoke]


def test_get_user_list_should_return_200_and_contract(user_api):
    """列表接口：状态码 + 结构契约 + 非空。"""
    resp = user_api.get_users()

    assert_status_code(resp, 200)
    assert_json_schema(resp, USER_LIST_SCHEMA, name="用户列表")
    assert len(resp.json()) > 0, "用户列表不应为空"


def test_user_list_should_have_exactly_10_records(user_api):
    """数据量校验：jsonplaceholder 固定返回 10 条用户。"""
    users = user_api.get_users().json()

    assert_field_equals(len(users), 10, "用户数量")


@pytest.mark.parametrize("user_id", [1, 2, 10], ids=lambda v: f"user-{v}")
def test_get_user_detail(user_api, user_id):
    """详情接口：返回的 id 必须与请求一致。"""
    resp = user_api.get_user(user_id)

    assert_status_code(resp, 200)
    assert_json_schema(resp, USER_SCHEMA, name=f"用户{user_id}详情")
    assert_field_equals(resp.json()["id"], user_id, "id")


def test_get_nonexistent_user_should_return_404(user_api):
    """异常场景：不存在的资源要返回 404，不能返回 200 + 空对象。

    这类「异常分支」才是 bug 高发区，面试时讲得出这条会加分。
    """
    resp = user_api.get_user(999999)

    assert_field_equals(resp.status_code, 404, "状态码")


def test_user_list_params_should_be_filtered(user_api):
    """参数传递校验：带 query 参数时，服务端过滤结果应符合预期。"""
    resp = user_api.get_users(id=3)

    assert_status_code(resp, 200)
    users = resp.json()
    assert all(u["id"] == 3 for u in users), f"过滤参数未生效: {users}"


@pytest.mark.parametrize(
    "field, expected_type",
    [
        ("id", int),
        ("name", str),
        ("username", str),
        ("email", str),
    ],
    ids=["id", "name", "username", "email"],
)
def test_user_fields_type(user_api, field, expected_type):
    """字段类型逐项校验：接口把 id 从 int 改成 string 是典型的前后端联调事故。"""
    users = user_api.get_users().json()
    wrong = [u for u in users if not isinstance(u.get(field), expected_type)]

    assert not wrong, f"字段 [{field}] 类型错误，样例: {wrong[:1]}"
