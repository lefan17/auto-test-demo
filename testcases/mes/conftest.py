"""MES Mock 业务的专用 fixture。

放在 testcases/mes/ 下的原因（不污染根 conftest）：
根 conftest 管的是"公共练习站"那套（jsonplaceholder / saucedemo），
本目录这套要起本地服务、连本地 SQLite。两者生命周期和依赖完全不同，
混在一个文件里会让 `pytest -m api` 也被迫加载 fastapi 相关的东西。
"""

from __future__ import annotations

import random

import pytest
from data_factory import SEED_BASELINE, DataFactory
from data_isolation import describe_diff, row_counts, snapshot
from mock_server_ctl import MockServer

from apis import MesApi


# ---------- 服务：整个测试会话起一次 ----------
@pytest.fixture(scope="session")
def mes_server(tmp_path_factory) -> MockServer:
    """session 级起一次 Mock 服务。

    端口由系统分配（避免残留进程占端口），数据库放在临时目录里，
    与开发者手工跑的那份 mock_server/dsh_mes.db 互不干扰。
    """
    tmp_dir = tmp_path_factory.mktemp("mes")
    db_path = tmp_dir / "mes_session.db"
    server = MockServer(db_path=db_path, log_path=tmp_dir / "server.log")
    server.start()
    try:
        yield server
    finally:
        # 无论成败都要停：CI 上留下僵尸 uvicorn 会让后续 job 卡在端口占用上
        server.stop()


@pytest.fixture(scope="session")
def mes_base_url(mes_server) -> str:
    return mes_server.base_url


# ---------- 用例级客户端与数据工厂 ----------
@pytest.fixture
def mes(mes_base_url) -> MesApi:
    return MesApi(mes_base_url)


@pytest.fixture(scope="session")
def mes_api(mes_base_url) -> MesApi:
    """session 级客户端：给"会话收尾要清理自己造的数据"这类用例用。

    用例级的 `mes` fixture 在用例结束就销毁了，收尾阶段还需要发请求，
    所以另给一个活到整个会话结束的实例。
    """
    return MesApi(mes_base_url)


@pytest.fixture
def factory(mes, mes_server) -> DataFactory:
    """数据工厂，并在用例结束后自动回收它造出来的数据。

    注意 fixture 里的 yield-teardown 顺序：清理必须在用例**结束之后**执行，
    这样即使断言失败也能收尾——否则一条失败的用例会留下脏数据，
    把后面几条用例一起带红，最后没人分得清哪个才是真正的失败点。
    """
    f = DataFactory(mes, mes_server.root_url, mes_server.db_path)
    yield f
    actions = f.cleanup()
    if actions:
        print("\n[数据回收] " + "；".join(actions))


# ---------- 隔离性断言用的基线 ----------
@pytest.fixture(scope="session")
def baseline_counts(mes_server) -> dict[str, int]:
    """服务启动（--reset 建库）后立刻抓一次基线行数。

    用实测而不是硬编码，是因为种子数据以后可能加料；
    硬编码的基线会在别人改种子时变成假失败。
    """
    measured = row_counts(mes_server.db_path)
    for table, expected in SEED_BASELINE.items():
        assert measured[table] == expected, (
            f"种子数据基线与预期不符：{table} 期望 {expected}，实际 {measured[table]}。"
            "这通常意味着别人改了 mock_server/db.py 的 SEED_* 而没同步这里的基线。"
        )
    return measured


@pytest.fixture(scope="session", autouse=True)
def data_isolation_check(mes_server) -> None:
    """整个会话跑完后，各表行数与库存可用量合计必须回到基线。

    `data_factory.py` 的模块注释把"数据隔离"写成了三条承诺，但在这之前
    没有任何一条用例在验证它 —— 承诺没有测试兜底，就是一句愿望。
    这里把它变成会红的测试：任何一条用例忘了清理自己造的数据，
    或者领了料没退回去，会话结束时就会失败，并且打印出是哪张表差了几行。

    为什么口径里除了行数还要看库存合计：
    领料/退料只 UPDATE 一行，行数不变但账实已经不符，行数指标抓不到这种泄漏。

    为什么挂在 session 级而不是每条用例结束都查：
    工厂方法造完数据、下一条用例还没跑时，行数本来就是"临时脏"的；
    逐条查会把正常流程判成污染。会话级才是"该还的东西都还了没有"的正确时点。
    """
    before = snapshot(mes_server.db_path)
    yield
    after = snapshot(mes_server.db_path)
    assert before == after, (
        "会话结束时数据没有回到基线 —— 有用例造了数据没清理，或改了库存没退回去。\n"
        f"{describe_diff(before, after)}\n"
        "排查方向：用了 factory 却绕开它直接发请求造数据（没登记回收）；"
        "或者领料用例忘了反向退料（见 test_mes_business.py 的 take_material fixture）。"
    )


# ---------- 用并发跑用例的辅助 ----------
@pytest.fixture
def race():
    """把若干无参函数**同时**发出去，返回 (结果列表, 墙钟毫秒)。

    实现要点：用一个 threading.Barrier 让所有线程在起跑线对齐后再发请求。
    为什么不能简单地"起 N 个线程然后 join"：
    线程创建本身有先后，第一个请求可能已经跑完，第 N 个才刚发出，
    这样根本构不成竞态——用例会"稳定通过"，但什么都没验证到。
    """
    import threading
    import time

    def run(funcs: list, workers: int | None = None):
        workers = workers or len(funcs)
        barrier = threading.Barrier(workers, timeout=30)
        results: list = [None] * workers
        errors: list = [None] * workers

        def wrapper(index: int, fn):
            try:
                barrier.wait()
                results[index] = fn()
            except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程
                errors[index] = exc

        threads = [threading.Thread(target=wrapper, args=(i, fn), daemon=True) for i, fn in enumerate(funcs)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if any(e is not None for e in errors):
            raise AssertionError(f"并发执行中有线程抛异常: {errors}")
        return results, elapsed_ms

    return run


@pytest.fixture
def unique_tag() -> str:
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=6))
