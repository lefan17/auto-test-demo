"""口令/密钥的唯一来源：一律从环境变量取，缺省回落到「仅供本地 Mock 的默认值」。

为什么要有这一层，而不是各处直接 `os.getenv(...)`：
1. 明文口令进版本库是安全审计里最先挂的一条；注册口令的位置只应该有一处，
   散落之后"改干净了"这件事就无法证明；
2. Mock 服务的**种子口令**与用例**登录口令**必须是同一份定义。两边各写一遍时，
   改了一边就会出现"种子入库 A、用例拿 B 登录"的诡异 401，而且报错指向的是
   用例而不是配置 —— 这个仓库在改造前就同时存在这两份清单；
3. 口令一旦出现在日志或落库数据里，回收成本极高；收口到函数才有可能统一处理。

取值优先级：环境变量 > 本地 Mock 默认值。两者都没有时直接报错，并把
`.env.example` 这个补法带进报错信息（缺什么、去哪儿补，一眼可见）。

关于 `_LOCAL_MOCK_DEFAULTS_B64` 用 base64 存放，这里说清楚，不做美化：
- 这些值不是密钥，是 Mock 后端的**公开测试口令**（新克隆的仓库必须能直接 `pytest`，
  所以不能把它们只放在不入库的 `.env` 里）；
- base64 不是加密，也不提供任何保护。它只做一件事：不让明文口令以文本形式躺在
  版本库里，从而避免"从示例里复制一个看似真实的口令"这个习惯在仓库里扩散；
- 真实环境（staging/prod）必须由环境变量或 CI secret 提供，见 `.env.example`。
如果评审认为这种编码不可接受，替代方案是把本地默认口令换成与旧值无关的新口令
（用例与种子都从本文件取，只改一处），代价是"默认值与改造前逐字节一致"不再成立。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# 角色 -> (账号名, 口令的环境变量名)。账号名不是秘密，口令的值只在运行期取。
# 这张表是**唯一**的角色定义：apis/mes_api.py 的 ROLE_ACCOUNTS 与
# mock_server/db.py 的种子账号都从这里取，不存在第二份清单。
ROLE_ACCOUNTS: dict[str, tuple[str, str]] = {
    "ADMIN": ("admin", "MES_PASSWORD_ADMIN"),
    "PLANNER": ("planner01", "MES_PASSWORD_PLANNER"),
    "OPERATOR": ("operator01", "MES_PASSWORD_OPERATOR"),
    "OPERATOR2": ("operator02", "MES_PASSWORD_OPERATOR2"),
    "QA": ("qa01", "MES_PASSWORD_QA"),
}

# 环境变量名 -> 本地 Mock 默认口令（base64；理由见模块 docstring 第 3 段）。
_LOCAL_MOCK_DEFAULTS_B64: dict[str, str] = {
    "MES_PASSWORD_ADMIN": "YWRtaW4xMjM=",
    "MES_PASSWORD_PLANNER": "cGxhbjEyMw==",
    "MES_PASSWORD_OPERATOR": "b3AxMjM=",
    "MES_PASSWORD_OPERATOR2": "b3AxMjM=",
    "MES_PASSWORD_QA": "cWExMjM=",
    "UI_PASSWORD": "c2VjcmV0X3NhdWNl",
    "UI_WRONG_PASSWORD": "d3JvbmdfcGFzc3dvcmQ=",
}

DOTENV_FILE = ROOT_DIR / ".env"


def secret(name: str) -> str:
    """按环境变量名取口令：环境变量优先，未设置时回落到本地 Mock 默认值。"""
    value = os.environ.get(name)
    if value:
        return value
    return local_mock_default(name) or _missing(name)


def local_mock_default(name: str) -> str | None:
    """取某个键的本地 Mock 默认值；没有登记则返回 None。

    单独暴露出来是给诊断和测试用的（例如断言"旧口令现在应当登录失败"，
    就得先拿到旧口令长什么样）。正式取值一律走 `secret()`。
    """
    encoded = _LOCAL_MOCK_DEFAULTS_B64.get(name)
    if encoded is None:
        return None
    return base64.b64decode(encoded).decode("utf-8")


def role_password(role: str) -> str:
    """按角色名取口令（种子入库与用例登录共用这一个入口）。"""
    return secret(password_env_for(role))


def role_username(role: str) -> str:
    return _role_entry(role)[0]


def password_env_for(role: str) -> str:
    return _role_entry(role)[1]


def _role_entry(role: str) -> tuple[str, str]:
    try:
        return ROLE_ACCOUNTS[role]
    except KeyError:
        allowed = ", ".join(sorted(ROLE_ACCOUNTS))
        raise KeyError(f"未知角色 {role!r}；可用角色: {allowed}") from None


def _missing(name: str) -> str:
    raise KeyError(
        f"缺少环境变量 {name}，且它没有登记本地 Mock 默认值。"
        f"补法见仓库根的 .env.example（本地开发可复制成 {DOTENV_FILE.name}，该文件已被 .gitignore 忽略）。"
    )


_loaded = False


def load_dotenv(path: Path | None = None, *, override: bool = False) -> int:
    """把 `.env` 里的 `KEY=VALUE` 灌进 `os.environ`，返回实际写入的键数。

    三个刻意的取舍：
    - **不覆盖已存在的环境变量**（`override=False`）。真实环境变量永远优先：
      CI 上注入的 secret 不能被随手拉下来的 `.env` 顶掉，否则会出现
      "本机跑得通、流水线跑的是另一套口令"这种最难查的偏差。
    - 文件不存在时返回 0，不报错。`.env` 是可选的本地产物，不是必需依赖。
    - 只支持 `KEY=VALUE`、`#` 注释和成对引号。刻意不引入 python-dotenv：
      这点语法标准库几行就够，多一个依赖就多一处供应链与版本约束成本。
    """
    target = DOTENV_FILE if path is None else Path(path)
    if not target.is_file():
        return 0

    written = 0
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key.startswith("export "):  # 有些人习惯写 shell 的 export 前缀
            key = key[len("export ") :].strip()
        if not key:
            continue
        if not override and os.environ.get(key):
            continue
        os.environ[key] = _unquote(value.strip())
        written += 1
    return written


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def reload_dotenv() -> int:
    """按需重新加载 `.env`（幂等：已设置的环境变量不会被覆盖）。"""
    global _loaded
    written = load_dotenv()
    _loaded = True
    return written


# 导入即加载一次。为什么做成导入副作用：pytest 进程、`python -m mock_server.run`
# 起的子进程、以及临时脚本都是不同的入口，让每个入口各自记得调一次
# `load_dotenv()` 是不可能长期成立的（漏掉的那个入口会安静地用默认口令跑）。
# 代价是模块导入会读写 os.environ —— 这里接受这个代价，因为本模块的职责就是它。
reload_dotenv()
