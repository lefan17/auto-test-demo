"""P0-1 的验收用例：口令真的从 provider 走，而不是"grep 干净了、行为没变"。

为什么必须有这一组用例：
"You can't grep the password out of the repo" 是一个**静态**结论，
它无法区分下面两种实现：
  A. 口令真的由 `common/secrets.py` 统一提供；
  B. 口令只是被挪进了另一个文件 / 换了种编码，取值路径根本没变。
B 完全能通过 `git grep` 零命中的验收，但一点安全收益都没有。
所以这里补上动态证据：改环境变量 -> 行为跟着变。

本文件覆盖两件事：
1. 环境变量优先于本地 Mock 默认值（provider 的行为）；
2. 换了口令之后，**种子入库与用例登录使用同一个口令**（这是"两处定义"的
   反面：改一处，服务端和客户端同时跟着变）。第 2 条是本组用例的承重项，
   它同时证明了"同一个来源"和"旧口令确实失效"。
"""

from __future__ import annotations

import os

import pytest
from mock_server_ctl import MockServer

from common.secrets import (
    DOTENV_FILE,
    ROLE_ACCOUNTS,
    load_dotenv,
    local_mock_default,
    role_password,
    role_username,
    secret,
)

pytestmark = [pytest.mark.mes, pytest.mark.api, pytest.mark.regression]


def test_role_passwords_come_from_environment(monkeypatch):
    """环境变量存在时，provider 必须返回它，而不是本地默认值。"""
    monkeypatch.setenv("MES_PASSWORD_ADMIN", "env-wins-123")
    assert role_password("ADMIN") == "env-wins-123"
    # 没设环境变量的角色不受影响，仍然回落到本地 Mock 默认值。
    assert role_password("QA") == local_mock_default("MES_PASSWORD_QA")


def test_local_mock_default_is_used_when_env_missing(monkeypatch):
    """不设环境变量时必须回落到默认值 —— 这是"新克隆的仓库能直接 pytest"的前提。"""
    monkeypatch.delenv("MES_PASSWORD_ADMIN", raising=False)
    assert role_password("ADMIN") == local_mock_default("MES_PASSWORD_ADMIN")
    assert role_password("ADMIN"), "本地 Mock 默认值不能是空字符串，否则登录必然失败"


def test_empty_env_value_falls_back_to_default(monkeypatch):
    """环境变量设成空串视为"没设"。

    为什么：`$env:MES_PASSWORD_ADMIN = ""` 在 Windows 上很容易手滑写出来，
    若把它当成有效口令，表现是"登录 401 且看不出原因"，
    而不是"明显少配了一个变量"。
    """
    monkeypatch.setenv("MES_PASSWORD_ADMIN", "")
    assert role_password("ADMIN") == local_mock_default("MES_PASSWORD_ADMIN")


def test_secret_reports_missing_key_with_pointer_to_template(monkeypatch):
    """没登记、也没设的键必须显式报错，并在信息里指出补法。"""
    monkeypatch.delenv("NOT_REGISTERED_KEY", raising=False)
    with pytest.raises(KeyError) as excinfo:
        secret("NOT_REGISTERED_KEY")
    assert "NOT_REGISTERED_KEY" in str(excinfo.value)
    assert ".env.example" in str(excinfo.value)


def test_unknown_role_lists_available_roles():
    with pytest.raises(KeyError) as excinfo:
        role_password("SUPERVISOR")
    assert "SUPERVISOR" in str(excinfo.value)
    for role in ROLE_ACCOUNTS:
        assert role in str(excinfo.value)


def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    """`.env` 不能顶掉已存在的环境变量。

    防的事故：CI 上注入了 secret，而某台机器的 `.env` 把它覆盖成另一个口令，
    于是同一份代码在流水线与本机跑的是两套凭据 —— 最难查的一类偏差。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("MES_PASSWORD_ADMIN=from-dotenv\nFRESH_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("MES_PASSWORD_ADMIN", "from-real-env")
    monkeypatch.delenv("FRESH_KEY", raising=False)

    written = load_dotenv(env_file)

    assert os.environ["MES_PASSWORD_ADMIN"] == "from-real-env", "已有环境变量被 .env 覆盖了"
    assert os.environ["FRESH_KEY"] == "from-dotenv", "缺失的键应从 .env 灌进来"
    assert written == 1


def test_dotenv_missing_file_is_not_an_error(tmp_path):
    """`.env` 是可选的本地产物；不存在时必须安静返回，而不是让整个测试会话起不来。"""
    assert load_dotenv(tmp_path / "definitely-missing.env") == 0
    assert not DOTENV_FILE.exists() or DOTENV_FILE.is_file()


def test_custom_admin_password_changes_both_seed_and_client(tmp_path, monkeypatch):
    """承重项：把管理员口令换成自定义值后，旧口令必须登录失败。

    这条用例同时验证三件事，缺一条它就会变红：
    1. 服务端的**种子口令**跟着环境变量走（否则服务端仍用旧口令，
       自定义口令会 401 → 第一个断言失败）；
    2. 客户端取的**登录口令**跟着环境变量走（否则客户端仍用旧口令，
       在"服务端也换了"的情况下同样 401 → 第一个断言失败）；
    3. 旧口令确实已经无效（第二个断言；若种子还认旧口令，这里就会红）。

    为什么必须**另起一个 Mock 服务**：种子口令是 init_db() 时算成哈希落库的，
    换环境变量不会改已入库的数据。共用会话级服务的话，这条用例只能验证
    "客户端换了口令 → 登录失败"，那恰恰是**坏实现**也会有的表现
    （客户端和服务端口令不一致），证明不了同源。
    """
    custom = "custom-admin-pw-9f3a"
    # 旧口令取自 provider 登记的本地 Mock 默认值，而不是写死在用例里 ——
    # 写死的话，"旧口令"这个概念就和实现脱钩了，改动后用例会静默失去意义。
    stale = local_mock_default("MES_PASSWORD_ADMIN")
    assert stale is not None, "provider 必须登记本地 Mock 默认值，否则本用例失去对照物"
    assert stale != custom

    monkeypatch.setenv("MES_PASSWORD_ADMIN", custom)
    username = role_username("ADMIN")
    server = MockServer(db_path=tmp_path / "custom_pw.db", log_path=tmp_path / "custom_pw.log")
    server.start(timeout=60.0)
    try:
        from apis import MesApi

        api = MesApi(server.base_url)
        fresh = api.login(username, custom)
        assert fresh.status_code == 200, (
            f"用自定义口令登录失败，说明种子口令没跟着 provider 走：{fresh.status_code} {fresh.text[:200]}"
        )

        old = api.login(username, stale)
        assert old.status_code == 401, f"旧口令仍然能登录，说明口令没有真正外置：{old.status_code} {old.text[:200]}"
    finally:
        server.stop()


def test_seed_usernames_match_client_role_table(tmp_path, monkeypatch):
    """种子表的账号名必须与客户端角色表一致，且库里角色取值合法。

    改了角色表却漏改种子表时，表现是"登录 401"，而报错指向用例 ——
    这两张表现在由 common/secrets.py 一起提供，这里把"一致"钉成断言。
    """
    from mock_server import db as mock_db

    assert [row[0] for row in mock_db.SEED_USERS] == [role_username(role) for role in ROLE_ACCOUNTS]
    assert [row[2] for row in mock_db.SEED_USERS] == [db_role for _, db_role in mock_db.SEED_USER_ROLES]
    allowed = {"ADMIN", "PLANNER", "OPERATOR", "QA"}
    assert set(row[2] for row in mock_db.SEED_USERS) <= allowed, "种子角色的取值必须落在 users 表的 CHECK 约束内"
