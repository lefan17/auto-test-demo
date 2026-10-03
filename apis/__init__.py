"""接口模块统一出口。

好处：用例里写 `from apis import UserApi`，以后拆分模块（user_api.py 一分为
user_api.py + role_api.py）时用例不用改 import。

这个包故意叫 `apis` 而不是 `api`：`testcases/api/` 是测试目录，
若源码包也叫 `api`，pytest 会把 `testcases/api/__init__.py` 抢先注册成 `api`，
于是 `from api import MesApi` 会静默解析到测试目录里去。
名字撞车不会提示"模块不存在"，只会给出一串看不懂的 ImportError。
"""

from apis.base import BaseApi
from apis.mes_api import ROLE_ACCOUNTS, ApiError, MesApi, RoleClient
from apis.user_api import PostApi, UserApi

__all__ = [
    "BaseApi",
    "UserApi",
    "PostApi",
    "MesApi",
    "RoleClient",
    "ApiError",
    "ROLE_ACCOUNTS",
]
