"""locust 场景定义：给 mock_server 出**容量结论**，而不是判定竞态。

与 `testcases/mes/concurrency.py` 的边界（两者不可互相替代）：

- `run_concurrently(funcs)` 用 `threading.Barrier` 对齐启动、用 `spans` 时间线交叠
  判定**竞态是否发生**，回答的是"会不会超卖/丢更新"，产出是布尔结论 + 时间线证据。
- 本文件用 locust 施加**持续的、按用户数分梯度的**负载，回答的是"多少并发下
  RPS 到顶 / P95 何时恶化 / 错误率从哪一档开始抬升"，产出是容量数字与拐点。
  竞态用例 2 个并发就能红，压测跑到 50 并发也可能全绿 —— 测的不是同一件事。

两个 User 类，由 `perf/run_load.py` **按类名指定**跑哪一个（不是靠权重分流）：

- `MesApiUser`   ：读写混合场景，任务权重 read 合计 8 : write 合计 2
- `LoginOnlyUser`：只压 `/auth/login`，量 PBKDF2(120_000 rounds) 的 CPU 天花板

为什么不让 locust 自动按权重分派这两个类：两个类权重相同就各占一半并发名额，
"50 用户"里会有 25 个在打登录、25 个在打业务 —— 两个场景的数字互相污染，
谁也读不出容量。用 `--tags` 过滤同样不行：被过滤空的那个类会打警告并在实例化时失败
（`locust/user/task.py:221-226`），而且它照样占用用户名额。

并发语义：`wait_time = constant(0)` —— 每个用户不做思考，发完立刻发下一条，
于是"用户数"≈"在途请求数"，量到的是**服务端吞吐上限**（这正是容量结论需要的口径）。
要"带思考时间的真实节奏"时设 `LOAD_THINK_TIME_MS=500`，并注意此时 RPS 主要由
思考时间决定、不再反映服务端容量。

为什么把仓库根插进 `sys.path`：`locust --locustfile perf/locustfile.py` 只会把
`perf/` 放进 `sys.path`，`import common.secrets` 会 ImportError。而登录口令在本仓库
有唯一来源（`common/secrets.py` 的 `ROLE_ACCOUNTS`），压测脚本必须复用那一份，
不能自己再写一遍账号口令清单 —— 那正是 `common/secrets.py:32-34` 点名要消灭的
"两份清单"。
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any

from locust import HttpUser, constant, task

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.secrets import ROLE_ACCOUNTS, role_username, secret  # noqa: E402

API_PREFIX = os.getenv("LOAD_API_PREFIX", "/api/v1").rstrip("/")
ROLE = os.getenv("LOAD_ROLE", "ADMIN").strip().upper()
THINK_TIME_MS = int(os.getenv("LOAD_THINK_TIME_MS", "0"))
LOGIN_NAME = "POST /auth/login"

# 写场景的载荷：MAT-SC-003(十字槽盘头螺钉) 种子可用量 3000.000
# （mock_server/db.py:210），单次领/退 0.001，领完立刻退回，所以长时间压测也不会
# 把库存打空 —— 打空会产出 409 库存不足，把错误率污染成"压测脚本的锅"而不是
# "服务的容量问题"。
WRITE_MATERIAL = os.getenv("LOAD_WRITE_MATERIAL", "MAT-SC-003")
WRITE_WAREHOUSE = os.getenv("LOAD_WRITE_WAREHOUSE", "WH-01")
WRITE_QTY = os.getenv("LOAD_WRITE_QTY", "0.001")
# 退料默认关闭：它是 check-then-act 的竞态点（详见 MesApiUser 的文档字符串），
# 混进混合场景会把"容量"和"竞态"两件事混成一个读不出结论的错误率。
INCLUDE_REVERSE = os.getenv("LOAD_INCLUDE_REVERSE", "0").strip() not in ("", "0", "false", "False")

PAGE_QUERY: dict[str, int] = {"page": 1, "page_size": 20}
WRITE_BODY: dict[str, str] = {
    "material_code": WRITE_MATERIAL,
    "warehouse": WRITE_WAREHOUSE,
    "qty": WRITE_QTY,
}


def _load_token_pool() -> list[str]:
    """读 `LOAD_TOKEN_POOL`（runner 预热的令牌池，JSON 数组）。

    有池就**不在压测期间登录**：一次 `/auth/login` 要花 PBKDF2 120k rounds 的 CPU
    （见 `mock_server/security.py` 的 `PBKDF2_ROUNDS`），50 个用户在 on_start 同时
    登录会造出一场与业务无关的 CPU 风暴，把业务接口的 P95 全带偏。池空时退化为
    on_start 登录，并在统计里以 `[setup] POST /auth/login` 单独命名，一眼能看出
    这批数字里混了登录。
    """
    raw = os.getenv("LOAD_TOKEN_POOL", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item]


_TOKEN_POOL = _load_token_pool()


def _login_payload() -> dict[str, str]:
    if ROLE not in ROLE_ACCOUNTS:
        raise RuntimeError(f"未知角色 {ROLE!r}，可选：{sorted(ROLE_ACCOUNTS)}")
    return {"username": role_username(ROLE), "password": secret(ROLE_ACCOUNTS[ROLE][1])}


def _login_once(client: Any) -> str | None:
    with client.post(
        f"{API_PREFIX}/auth/login",
        json=_login_payload(),
        name="[setup] POST /auth/login",
        catch_response=True,
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"取令牌失败 status={resp.status_code} body={resp.text[:200]}")
            return None
        resp.success()
        body = resp.json()
    token = body.get("access_token") if isinstance(body, dict) else None
    return str(token) if token else None


class _AuthMixin:
    """需要令牌的场景共用取令牌逻辑。

    故意**不继承 HttpUser**：locust 会把每个 User 子类都实例化并占用并发名额，
    一个没有任务的 User 基类会被当成"站在那儿不动的用户"，把名额白白吃掉。
    """

    client: Any
    wait_time = constant(THINK_TIME_MS / 1000.0)

    def on_start(self) -> None:
        token = random.choice(_TOKEN_POOL) if _TOKEN_POOL else _login_once(self.client)
        if token is None:
            raise RuntimeError(
                "拿不到访问令牌，压测无法开始。检查服务是否已起、种子账号口令是否被环境变量改过、"
                "LOAD_TOKEN_POOL 是否已失效。"
            )
        self.client.headers.update({"Authorization": f"Bearer {token}"})


class MesApiUser(_AuthMixin, HttpUser):
    """读写混合场景。read 合计权重 8、write 权重 1。

    写场景**只领料、不退料**，这是被实测证据逼出来的设计，不是省事：
    首版把 `POST /inventory/reverse` 也放进混合场景，10 用户 10 秒就稳定产出
    13/3011 = 0.43% 的失败，5 用户是 34/3451 = 0.99%，失败明细全是
    `POST /inventory/reverse` 的 `400 Bad Request`
    （`reports/perf/20261014-234155-smoke4/u010-r1_failures.csv`）。
    400 来自 `mock_server/services.py:636` 的 `if row["issued_milli"] < qty`：
    退料是 "读 issued_milli → 判断 → 写回" 的 check-then-act，且没有并发窗口
    保护；多用户下别人的退料会把它读到的值作废，于是退料量超过当时真的已发量。

    也就是说，那个 400 **不是容量问题，而是一场竞态**——正是
    `testcases/mes/concurrency.py` 存在的理由。把它留在压测里，量出来的
    "错误率" 会变成两种东西的混合物（服务吞吐不足 + 压测脚本自己踩中竞态），
    这个数字对容量决策没有用。边界因此划在这里：**压测发不退料，竞态交给竞态用例**。
    退料接口的延迟数据由可选场景 `LOAD_INCLUDE_REVERSE=1` 单独采集，
    那时要读的是延迟，不是错误率。
    """

    @task(2)
    def stock_list(self) -> None:
        self.client.get(f"{API_PREFIX}/inventory/stock", params=PAGE_QUERY, name="GET /inventory/stock")

    @task(2)
    def material_list(self) -> None:
        self.client.get(f"{API_PREFIX}/materials", params=PAGE_QUERY, name="GET /materials")

    @task(2)
    def work_order_list(self) -> None:
        self.client.get(f"{API_PREFIX}/work-orders", params=PAGE_QUERY, name="GET /work-orders")

    @task(2)
    def ledger_list(self) -> None:
        self.client.get(f"{API_PREFIX}/inventory/ledger", params=PAGE_QUERY, name="GET /inventory/ledger")

    @task(1)
    def issue_material(self) -> None:
        self.client.post(f"{API_PREFIX}/inventory/issue", json=WRITE_BODY, name="POST /inventory/issue")

    if INCLUDE_REVERSE:

        @task(1)
        def reverse_material(self) -> None:
            self.client.post(f"{API_PREFIX}/inventory/reverse", json=WRITE_BODY, name="POST /inventory/reverse")


class LoginOnlyUser(HttpUser):
    """只压登录。

    单独成一个场景，是因为它和业务接口抢的是同一种资源（CPU），混在一起测不出
    "登录本身能扛多少"，而这恰恰是登录风暴事故里最先趴下的一环。
    """

    wait_time = constant(THINK_TIME_MS / 1000.0)

    @task
    def login(self) -> None:
        self.client.post(f"{API_PREFIX}/auth/login", json=_login_payload(), name=LOGIN_NAME)
