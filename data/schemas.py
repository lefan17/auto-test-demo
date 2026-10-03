"""用户接口的 JSON Schema 契约。

为什么单独放文件：响应结构是「契约」，用例断言它、开发改坏它。
结构定义和用例逻辑分开，接口字段变了只改这里。
"""

# 注意：properties 只写「必填且类型固定」的字段；不写 required 的字段就是可选，
# 这样接口新增字段时用例不会误报失败（契约测试要防的是「删字段/改类型」，不是「加字段」）。
USER_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "username", "email"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "username": {"type": "string"},
        "email": {"type": "string"},
        "address": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "zipcode": {"type": "string"},
            },
        },
        "phone": {"type": "string"},
        "website": {"type": "string"},
    },
}

USER_LIST_SCHEMA = {"type": "array", "items": USER_SCHEMA}

# 创建用户后的响应（服务端会补上 id）
USER_CREATED_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "username", "email"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "username": {"type": "string"},
        "email": {"type": "string"},
    },
}

POST_SCHEMA = {
    "type": "object",
    "required": ["id", "userId", "title", "body"],
    "properties": {
        "id": {"type": "integer"},
        "userId": {"type": "integer"},
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
}
