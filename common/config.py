"""环境分层配置加载器：dev / staging / prod 三套档案，用 `TEST_ENV` 选择。

为什么 dev 档不是新写的 `config.dev.yaml`，而是复用现有的 `config.yaml`：
这个仓库在被加上分层之前，所有人和所有脚本都从 `data/config.yaml` 读配置
（README、setup.ps1、conftest.py 的说明，以及别人已经拉下来的分支）。
把它改名成 `config.dev.yaml` 会让这些引用**一次性全部失效**——
为了目录整齐而制造一次全仓断链，是分层改造里最不值得付出的代价。
所以 dev 档就是 `config.yaml` 本身：`TEST_ENV=dev` 与不设 `TEST_ENV`
指向同一个文件，默认行为与加分层之前逐字节一致。

为什么用环境变量而不是命令行参数：
这个仓库跑 pytest 的方式有很多种（裸 `pytest`、`python -m pytest`、CI 里分四次跑、
IDE 里点运行）。环境变量对调用方式零依赖；命令行参数则要在每一处都记得传，
漏一处就悄悄跑回默认环境——这比不提供分层更危险。
传输层的 `API_TIMEOUT` / `API_RETRIES` 用的也是同一套机制，口径统一。

为什么不在返回的 dict 里塞一个 `profile` 键：
`get_config()` 的返回值会被下游当成"配置文件的内容"使用。
默认路径下多出一个文件里没有的键，会让"配置来自文件"这句话变得不精确，
也可能碰上按文件内容做等值比较的代码。分层靠**文件不同**来表达，
不靠往数据里注入标记。
"""

from __future__ import annotations

import os

from common.yaml_util import load_yaml

# profile 名 -> data/ 目录下的文件名。
# dev 复用既有 config.yaml，不制造第二份内容相同的档案（重复的档案必然漂移）。
PROFILE_FILES = {
    "dev": "config.yaml",
    "staging": "config.staging.yaml",
    "prod": "config.prod.yaml",
}
DEFAULT_PROFILE = "dev"
ENV_VAR = "TEST_ENV"


def resolve_profile() -> str:
    """从环境变量解析出当前 profile 名，未设置时取 dev（即旧行为）。"""
    return _normalize(os.getenv(ENV_VAR, DEFAULT_PROFILE))


def get_config(profile: str | None = None) -> dict:
    """读取当前环境档案的配置。profile 显式传入时优先于环境变量。"""
    name = _normalize(profile) if profile is not None else resolve_profile()
    return load_yaml(PROFILE_FILES[name])


def _normalize(raw: str) -> str:
    # 大小写和首尾空格都容错：Windows 上 `$env:TEST_ENV = "Prod "` 这种写法太容易出现了，
    # 而"环境变量写错"不该表现成"配置没生效"这种需要排查半小时的现象。
    name = (raw or "").strip().lower()
    if name not in PROFILE_FILES:
        allowed = ", ".join(sorted(PROFILE_FILES))
        raise ValueError(f"未知的 {ENV_VAR} 档案: {raw!r}；可用档案: {allowed}")
    return name
