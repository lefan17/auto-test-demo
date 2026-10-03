"""接口模块层：按业务模块划分，一个模块一个类。

用例只调这里的方法（get_user(1)），不关心 URL 长什么样。
新增一个接口 = 在这里加一个方法。
"""

from requests import Response

from apis.base import BaseApi


class UserApi(BaseApi):
    """用户模块接口。"""

    def get_users(self, **params) -> Response:
        return self.get("/users", params=params)

    def get_user(self, user_id: int) -> Response:
        return self.get(f"/users/{user_id}")

    def create_user(self, payload: dict) -> Response:
        return self.post("/users", json=payload)

    def update_user(self, user_id: int, payload: dict) -> Response:
        return self.put(f"/users/{user_id}", json=payload)

    def delete_user(self, user_id: int) -> Response:
        return self.delete(f"/users/{user_id}")


class PostApi(BaseApi):
    """文章模块接口。"""

    def get_posts(self, **params) -> Response:
        return self.get("/posts", params=params)

    def get_post(self, post_id: int) -> Response:
        return self.get(f"/posts/{post_id}")

    def create_post(self, payload: dict) -> Response:
        return self.post("/posts", json=payload)
