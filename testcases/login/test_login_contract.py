"""登录接口自动化测试用例（LC-01 ~ LC-30，落地 27 条）。

对应用例设计文档：docs/login-test-cases.md
被测服务：login_app/（见 login_app/app.py 的"6 个发现点"说明）

## 这一套用例和本项目其它用例的区别

`testcases/api/` 和 `testcases/mes/` 断言的是**"系统是对的"**；
本文件断言的是**"系统的行为就是这样，包括它的缺陷"**。

全绿不代表被测系统没毛病，只代表它的行为没有被悄悄改掉。
想验证缺陷本身，看每条用例标题里的 FIND 编号。

## 运行

    pytest -m login                 # 只跑这一套（要起本地服务，无需公网）
    pytest testcases/login -v       # 等价写法

## 设计方法分布（对应 docs/login-test-cases.md 的表格）

    等价类划分  LC-01~LC-13   有效/无效输入各类取代表值
    边界值分析  LC-15~LC-21   边界上/内/外的值
    判定表      LC-22~LC-25   用户名、密码的有无 × 密码对错 的条件组合
    场景法      LC-26~LC-29   按业务流程与协议层走
    错误推测    LC-20/21/30   凭经验猜"容易出错的地方"

## 关于"发现点"断言为什么写成断言当前行为

真实项目里的缺陷不会等你测完才出现 —— 你发现了它，提了单，它可能因为排期
而暂时不修。这时候用例有两种写法：

1. `assert r.status_code == 400`（期望的正确行为）—— 用例常年红着，
   团队很快学会"这几条是已知失败，忽略即可"，然后真的失败也没人看了；
2. `assert r.status_code == 200  # FIND-02`（钉住当前行为）—— 用例是绿的，
   但一旦有人"顺手修好了"或者"改坏了"，这条用例立刻报警。

本套用第 2 种。缺陷本身记录在 docs/test_report_template.md 的缺陷清单里，
用例的职责是**当行为发生变化时立刻告诉你**。
"""

from __future__ import annotations

import pytest

# 整个文件打一组标记：
#   login —— 便于 `pytest -m login` 单独跑这一套
#   api   —— 它与 testcases/api/ 一样是接口层用例，跑接口回归时应一并带上
pytestmark = [pytest.mark.login, pytest.mark.api]

# ============================================================================
# 一、等价类划分 · 有效输入（LC-01 ~ LC-05）
# ============================================================================


def test_lc01_login_success(login_api):
    """LC-01 正确用户名 + 正确密码 -> 200，返回 token 与 user。"""
    r = login_api.login_json({"username": "admin", "password": "123456"})
    assert r.status_code == 200
    body = r.json()
    # 断言到具体字段值，而不是只断 200：
    # 只断状态码是最弱的断言，接口返回成功但字段全错也能过。
    assert body["token"] == "demo-token-123"
    assert body["user"] == "admin"


def test_lc02_any_username_can_login(login_api):
    """LC-02 【FIND-01】任意用户名 + 正确密码都能登录 —— 无用户存在性校验。

    这是本套用例里级别最高的发现点（中）：等于没有认证，
    只要密码是 123456，谁都能以任意身份进来。
    这里断言 200 是**钉住当前行为**，不是认可它。
    """
    r = login_api.login_json({"username": "nobody_here", "password": "123456"})
    assert r.status_code == 200, "行为变了：FIND-01 似乎已被修复，请更新用例与缺陷清单"
    assert r.json()["user"] == "nobody_here"


def test_lc03_username_is_trimmed(login_api):
    """LC-03 用户名前后空格被 trim 掉。"""
    r = login_api.login_json({"username": "  admin  ", "password": "123456"})
    assert r.status_code == 200
    assert r.json()["user"] == "admin"


def test_lc04_unicode_username(login_api):
    """LC-04 中文用户名可行（编码处理正常）。"""
    r = login_api.login_json({"username": "测试用户", "password": "123456"})
    assert r.status_code == 200
    assert r.json()["user"] == "测试用户"


def test_lc05_form_submission_accepted(login_api):
    """LC-05 【FIND-02】表单提交也能登录 —— 契约只定义了 application/json。

    契约偏差里属于"宽松"方向的一类：客户端一旦依赖上这个未定义行为，
    服务端哪天收紧成严格 JSON，线上就会突然炸。
    """
    r = login_api.login_form({"username": "admin", "password": "123456"})
    assert r.status_code == 200, "行为变了：FIND-02 似乎已被修复（不再兼容表单），请更新用例与契约"
    assert r.json()["user"] == "admin"


# ============================================================================
# 二、等价类划分 · 无效输入（LC-06 ~ LC-13）：契约要求 400
# ============================================================================


@pytest.mark.parametrize(
    "payload,case",
    [
        ({}, "LC-06 缺全部字段"),
        ({"password": "123456"}, "LC-07 缺 username"),
        ({"username": "admin"}, "LC-08 缺 password"),
        ({"username": "", "password": "123456"}, "LC-09 用户名为空字符串"),
        ({"username": "   ", "password": "123456"}, "LC-10 用户名全空白"),
        ({"username": "admin", "password": ""}, "LC-11 密码为空字符串"),
        ({"username": "admin", "password": None}, "LC-12 密码为 null"),
        ({"username": "admin", "password": 0}, "LC-13 密码为数字 0（falsy 被当成没传）"),
    ],
)
def test_lc06_to_lc13_missing_fields_400(login_api, payload, case):
    """LC-06~LC-13 必填字段缺失或为空 -> 400 + error 字段。

    用参数化而不是写 8 个函数：8 条用例的逻辑完全一样（发请求、断状态码、
    断 error 存在），只有数据不同。写 8 遍除了让文件更长没有任何收益 ——
    这也是"数据驱动"最该用的场合：**同构的用例**。
    """
    r = login_api.login_json(payload)
    assert r.status_code == 400, f"{case} 应返回 400，实际 {r.status_code}：{r.text}"
    assert "error" in r.json()


# ============================================================================
# 三、密码错误（LC-14 ~ LC-17）：契约要求 401
# ============================================================================


@pytest.mark.parametrize(
    "password,case",
    [
        ("wrong", "LC-14 完全错误的密码"),
        ("1234567", "LC-15 密码多一位（边界外）"),
        ("12345", "LC-16 密码少一位（边界外）"),
        ("123456 ", "LC-17 【FIND-03】密码不 trim，末尾空格视为不同密码"),
    ],
)
def test_lc14_to_lc17_wrong_password_401(login_api, password, case):
    """LC-14~LC-17 密码不对 -> 401。

    LC-17 是 FIND-03 的证据：用户名做了 trim 而密码没有，
    同一份输入两个字段处理不对称 —— 用户"明明输对了却登不上"的经典来源。
    """
    r = login_api.login_json({"username": "admin", "password": password})
    assert r.status_code == 401, f"{case} 应返回 401，实际 {r.status_code}：{r.text}"


# ============================================================================
# 四、边界值（LC-18 ~ LC-21）
# ============================================================================


def test_lc18_oversized_username_accepted(login_api):
    """LC-18 【FIND-04】1000 字符用户名照样登录成功 —— 无长度限制。

    超长输入不校验的后果不只是"不好看"：它可以是内存放大、日志爆量、
    以及下游截断不一致（这里存 1000 字符，下游存了前 64 个）。
    """
    r = login_api.login_json({"username": "a" * 1000, "password": "123456"})
    assert r.status_code == 200, "行为变了：FIND-04 似乎已被修复（加了长度限制），请更新用例"
    assert len(r.json()["user"]) == 1000


def test_lc19_oversized_password_rejected(login_api):
    """LC-19 1000 字符密码 -> 401（长度不匹配，走的是密码校验而非长度校验）。"""
    r = login_api.login_json({"username": "admin", "password": "a" * 1000})
    assert r.status_code == 401


def test_lc20_username_echoed_verbatim(login_api):
    """LC-20 【FIND-05】用户名原样回显 —— 前端若不转义即为 XSS。

    断言里刻意用 `==` 比对完整脚本串：只要哪天服务端开始转义或过滤，
    这条会立刻变红，正好提示"FIND-05 可能被修了"。
    """
    evil = "<script>alert('xss')</script>"
    r = login_api.login_json({"username": evil, "password": "123456"})
    assert r.status_code == 200
    assert r.json()["user"] == evil, "行为变了：用户名不再原样回显，请复查 FIND-05"


def test_lc21_sql_injection_probe(login_api):
    """LC-21 【错误推测】注入串没有触发注入，但再次暴露"无用户校验"。

    这一条值得说清楚它**没有**证明什么：服务端压根不查数据库，
    所以"注入没成功"说明不了它有防注入能力，只是这条路本来就没通。
    把这一点写在用例里，是为了避免以后有人拿它当"安全测试已覆盖"的证据。
    """
    r = login_api.login_json({"username": "admin' OR '1'='1", "password": "123456"})
    assert r.status_code == 200
    # 原样回显说明服务端没有做任何输入处理；它不查库，所以这不是注入漏洞，
    # 但同样说明"输入会被直接拿去用"。
    assert r.json()["user"] == "admin' OR '1'='1"


# ============================================================================
# 五、判定表（LC-22 ~ LC-25）：条件组合
# ============================================================================


def test_lc22_to_lc25_decision_table(login_api):
    """LC-22~LC-25 用户名/密码的有无 × 密码对错，四组组合全覆盖。

    判定表的价值在于**保证组合不遗漏**。这里只有 2 个条件、各 2 种取值，
    4 组就能穷举；条件一多（比如加上"账号是否锁定""是否过期"）就需要
    用正交或 Pairwise 去裁剪，否则组合数爆炸。
    """
    cases = [
        ({"username": "admin", "password": "123456"}, 200, "LC-22 有用户名+有密码+密码正确"),
        ({"username": "admin", "password": "bad"}, 401, "LC-23 有用户名+有密码+密码错误"),
        ({"password": "123456"}, 400, "LC-24 无用户名+有密码"),
        ({"username": "admin"}, 400, "LC-25 有用户名+无密码"),
    ]
    for payload, expected, desc in cases:
        r = login_api.login_json(payload)
        assert r.status_code == expected, f"{desc} 应返回 {expected}，实际 {r.status_code}"


# ============================================================================
# 六、场景法 / 协议层（LC-26 ~ LC-29）
# ============================================================================


def test_lc26_token_is_nonempty_string(login_api):
    """LC-26 token 是非空字符串（结构断言，不认具体值）。"""
    r = login_api.login_json({"username": "admin", "password": "123456"})
    token = r.json()["token"]
    assert isinstance(token, str)
    assert token, "token 不能是空字符串"


def test_lc27_token_is_fixed_value(login_api):
    """LC-27 【FIND-06】连续两次登录拿到的 token 完全相同 —— 固定 token。

    固定 token 的严重性在于：它没有签发时间、没有过期、无法撤销，
    一旦泄露就是永久有效。生产环境绝对不能这样。
    这里断言两次结果相等**且等于那个固定字面量**，是为了让"token 变成随机值"
    这个改动能被立刻发现（那意味着 FIND-06 被修了）。
    """
    t1 = login_api.login_json({"username": "admin", "password": "123456"}).json()["token"]
    t2 = login_api.login_json({"username": "admin", "password": "123456"}).json()["token"]
    assert t1 == t2, "token 不再是固定值，请复查 FIND-06 并更新用例"
    assert t1 == "demo-token-123"


def test_lc28_get_method_not_allowed(login_api):
    """LC-28 GET 访问登录接口 -> 405（方法不允许）。"""
    r = login_api.session.get(f"{login_api.base_url}/api/login", timeout=10)
    assert r.status_code == 405


def test_lc29_non_json_body_rejected(login_api):
    """LC-29 非 JSON 请求体（text/plain）-> 400。

    这一条钉的是**框架层面的行为**：FastAPI/Starlette 默认对解析失败的请求体
    返回 422，而契约要求 400。实现里手工读 body 就是为了保住 400 ——
    如果哪天有人图省事改成 pydantic 模型，这条会立刻变红。
    """
    r = login_api.login_raw("not-json", content_type="text/plain")
    assert r.status_code == 400, (
        f"非 JSON 请求体应返回 400（契约要求），实际 {r.status_code}。"
        "若变成 422，说明实现改用了 pydantic 自动校验，契约里的 400 语义丢了。"
    )


# ============================================================================
# 七、错误推测（LC-30）
# ============================================================================


def test_lc30_extra_fields_ignored(login_api):
    """LC-30 多余字段被忽略（宽松处理），不影响登录。

    "传了没定义的字段会怎样"是错误推测法的经典素材：
    有的实现严格拒绝、有的静默忽略、有的会把 role 这种字段直接写进用户对象
    （那就成了越权）。这里实测是忽略。
    """
    r = login_api.login_json({"username": "admin", "password": "123456", "role": "admin"})
    assert r.status_code == 200
    assert r.json() == {"token": "demo-token-123", "user": "admin"}, (
        "响应多了或少了字段 —— 若 role 之类的额外字段出现在响应里，属于越权风险"
    )


# ============================================================================
# 八、契约一致性（附加，不属于 LC 编号）
# ============================================================================


def test_health_endpoint_matches_contract(login_api):
    """健康检查返回的版本号要与契约一致。

    这一段不在原来的 30 条用例设计里，是搬过来时补的：
    docs/login-test-cases.md 是一份**契约驱动**的用例设计文档，
    而契约本身有没有被实现遵守，需要一条独立的用例来守。
    """
    from contract import CONTRACT

    r = login_api.health()
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] in CONTRACT["title"] or body["version"] == "1.0.0"


def test_undocumented_status_codes_are_still_undocumented(login_api):
    """提醒：契约里没写、但实现会返回的状态码，是有意留白的清单。

    这条用例的断言很"轻"，它的作用不是验证行为，而是**把一件事说清楚**：
    405/422/500 这些状态码没有写进契约。以后有人要加接口，先从这份清单开始想。
    """
    from contract import undeclared_status_codes

    codes = undeclared_status_codes()
    assert 405 in codes
    # 405 我们已经在 LC-28 里测了行为，但契约文档没写 —— 这个差异本身就是信息。
