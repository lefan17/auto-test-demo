"""接口模块统一出口。

好处：用例里写 `from api import UserApi`，以后拆分模块（user_api.py 一分为
user_api.py + role_api.py）时用例不用改 import。
"""
from api.base import BaseApi
from api.user_api import PostApi, UserApi

__all__ = ["BaseApi", "UserApi", "PostApi"]
