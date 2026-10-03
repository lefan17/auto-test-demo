"""DSH MES Mock API：给自动化用例提供"有真实业务约束"的后端。

为什么需要它（也是这个作品集最想讲的一件事）：
jsonplaceholder / saucedemo 这类公共练习站**没有业务语义**——它不会因为
库存不足拒绝下单，也不会因为重复提交产生两条订单。所以打在这类站点上的用例
无论写多少条，都只能验证"HTTP 通不通、字段在不在"，验证不了"业务对不对"。

这个 Mock 后端刻意把真实 MES/WMS 才有的约束做进去：
唯一性、状态机、库存边界、金额精度、幂等、角色权限、分页排序白名单、软删除。
约束全部在服务端真实生效并返回 4xx。

它不是生产系统：业务规则是自定的，并发窗口是人工造的（为了可复现）。
它的价值在于给用例提供**能被违反的规则**——没有规则，就没有测试。
"""

from mock_server.db import configure, ensure_ready, init_db

__all__ = ["app", "create_app", "API_PREFIX", "configure", "ensure_ready", "init_db"]
__version__ = "1.0.0"


def __getattr__(name: str):
    """延迟到真正访问时才构造 ASGI 应用。

    为什么必须延迟：`from mock_server import create_app` 是测试和运维脚本的
    常规写法，而 `create_app()` 会去打开数据库。如果包导入阶段就建 app，
    那么"只初始化数据库"这种只想做一半事情的命令也必然先连库——
    在没有库文件、只读挂载、或只想换一个 db 路径的情况下直接崩。
    """
    if name in ("app", "create_app", "API_PREFIX"):
        from mock_server import app as app_module

        if name == "API_PREFIX":
            return app_module.API_PREFIX
        if name == "create_app":
            return app_module.create_app
        return app_module.app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
