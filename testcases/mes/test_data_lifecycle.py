"""数据生命周期：失败兜底、TTL 清理、幂等、与会话级基线断言的衔接。

这个文件回答四个问题，每个问题都给出**能跑出证据**的用例而不是声明：
1. 用例中途抛异常时，已造数据还会被回收吗？—— 靠真实子进程跑一条会失败的探针，
   再看它跑完之后会话级基线断言有没有跟着红（验收 2）。
2. 清理不彻底能被现有断言抓到吗？—— 同样用真实子进程跑一条"故意漏登记"的探针（验收 5）。
3. TTL 清理能识别过期测试数据、又不动基线吗？—— 在独立的临时库上做，
   给出清理前后的 row_counts（验收 3），并连跑两次证明幂等（验收 4）。
4. 凭什么区分"测试数据"和"基线数据"？—— 命名判据本身也要有测试。

为什么故障注入要走**子进程**：
这两条探针的期望结果就是失败。把失败当成数据来断言，就必须在**另一个 pytest 进程**里
收集它的退出码、stdout 与"会话收尾有没有爆基线漂移"。直接用 pytest 的 API 在进程内跑
会让会话级 fixture 的状态漏进本用例所在会话，两边的结论混在一起就没法归因了。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from data_factory import SEED_MATERIALS, SEED_STOCK, SEED_USERS, DataFactory
from data_isolation import row_counts
from data_ttl import (
    BASELINE_MATERIALS,
    BASELINE_STOCK,
    BASELINE_USERS,
    is_test_code,
    lingering_rows,
    parse_ts,
    purge,
)

pytestmark = [pytest.mark.mes, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[2]
FAULT_PROBE = "testcases/mes/_fault_probe.py"
LEAK_PROBE = "testcases/mes/_leak_probe.py"


def _run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """在真子进程里跑真 pytest，返回它的结果（退出码/stdout/stderr）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


# ---------- 1&2：故障注入与"漏登记会被抓到" ----------
def test_failing_case_still_reclaims_its_data() -> None:
    """用例失败后数据仍被回收 —— 而且会话级基线断言**没有**跟着红。

    探针命中的是真实的兜底缺陷：fixture 早先是"先断言流水链、再 cleanup()"，
    于是流水链一红，`cleanup()` 就永远走不到，用例造的数据全留在库里，
    会话收尾再报一次"数据没有回到基线" —— 两个红，真正的原因反而被盖住。

    所以判据有两半，缺一不可：
    1. 探针必须失败，且失败信息必须是**流水链**（故障真的注进去了）；
    2. 基线漂移必须**不**出现（说明回收照跑，没有被断言挤掉）。
    只断言第 1 半就是在验证"异常会传播"，那是无关的事实。
    """
    proc = _run_pytest(FAULT_PROBE)
    out = proc.stdout

    assert proc.returncode != 0, f"探针本来就应该失败，却绿了；故障没注进去：\n{out}"
    assert "账实不符" in out, f"失败原因不是流水链，探针没命中要验的那条断言：\n{out}"
    assert "[数据回收]" in out, f"f.cleanup() 没有执行 —— 兜底又被断言挤掉了：\n{out}"
    assert "会话结束时数据没有回到基线" not in out, (
        "用例失败后数据没有被回收干净，会话级基线断言连带变红了 —— 兜底没生效：\n" + out
    )
    # fixture teardown 里报出来的失败在 pytest 里记作 error 而非 failed（用例正文是过的），
    # 两种都要认：这条探针的"正文"本来就该走完，红的是收尾那一步。
    assert "1 failed" in out or "1 error" in out, f"预期恰好一条失败/错误，实际不同：\n{out}"


def test_unregistered_data_is_caught_by_session_baseline() -> None:
    """绕开工厂造的、没登记回收的数据，会被会话级基线断言抓住（清理不彻底必须变红）。

    函数级兜底只能回收"登记过的"数据 —— 这是它的边界，不是缺陷。越过边界的部分
    必须有人接着兜，否则"清理不彻底"会安静地漂到下一轮。
    这条用例钉的就是那个接手的断言：它必须红，而且红的必须是基线漂移本身。
    """
    proc = _run_pytest(LEAK_PROBE)
    out = proc.stdout

    assert proc.returncode != 0, f"故意漏登记的探针竟然绿了，说明基线断言没抓住残留：\n{out}"
    assert "会话结束时数据没有回到基线" in out, f"缺了基线漂移的失败信息，抓到它的原因不对：\n{out}"
    assert "work_orders" in out, f"失败信息里没点出是哪张表差了行数：\n{out}"


# ---------- 3&4：TTL 清理 ----------
@pytest.fixture
def ttl_db(tmp_path, monkeypatch) -> Path:
    """一份独立的、已写种子的临时库。

    绝不在会话共享库上做删改：那正是本任务要修的那类污染。
    用 monkeypatch 而不是 `mock_server.db.configure()`，是为了让进程内的库路径
    在用例结束后自动还原 —— 手动还原漏一处就会让后面的用例连到别人的库上。
    """
    from mock_server import db as mock_db

    path = tmp_path / "ttl.db"
    monkeypatch.setattr(mock_db, "_DB_PATH", path)
    monkeypatch.setattr(mock_db._local, "db_path", path, raising=False)
    mock_db.init_db(reset=True)
    return path


def _backdate(db_path: Path, table: str, column: str, value: str, *, where: str = "1=1") -> None:
    """把某些行的创建时间改到过去，制造"过期"。

    这是本用例唯一能控制时间方向的杠杆：TTL 判定读的就是库里的 created_at。
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE {where}", (value,))
        conn.commit()
    finally:
        conn.close()


def _insert_material(db_path: Path, code: str, created_at: str) -> None:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(
            "INSERT INTO materials (material_code, name, category, unit, unit_price_cent,"
            " safety_stock_milli, created_at, updated_at) VALUES (?, ?, '测试料', 'KG', 1234, 0, ?, ?)",
            (code, f"测试物料 {code}", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO stock (material_code, warehouse, available_milli, updated_at) VALUES (?, 'WH-01', 1000, ?)",
            (code, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_order(db_path: Path, order_no: str, product_code: str, created_at: str) -> int:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        cur = conn.execute(
            "INSERT INTO work_orders (order_no, product_code, product_name, planned_qty_milli,"
            " status, created_at, updated_at) VALUES (?, ?, '测试产品', 10000, 'APPROVED', ?, ?)",
            (order_no, product_code, created_at, created_at),
        )
        order_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO production_batches (batch_no, work_order_id, planned_qty_milli, line,"
            " operator, created_at, updated_at) VALUES (?, ?, 10000, 'L1', '测试员', ?, ?)",
            (f"B{order_no}", order_id, created_at, created_at),
        )
        conn.execute(
            "INSERT INTO stock_ledger (material_code, warehouse, txn_type, qty_milli, balance_milli,"
            " work_order_no, operator, created_at) VALUES (?, 'WH-FG', 'RECEIPT', 10000, 10000, ?, '测试员', ?)",
            (product_code, order_no, created_at),
        )
        conn.commit()
        return order_id
    finally:
        conn.close()


OLD = "2020-01-01T00:00:00+08:00"


def test_purge_removes_expired_test_data_and_keeps_baseline(ttl_db: Path) -> None:
    """TTL 清理识别并删掉过期测试数据，同时一行基线都不动。

    时间基准用 `2020-01-01` 造"过期"。种子行的 created_at 是**建库那一刻**，
    在 TTL=24h 的口径下它们同样"过期" —— 这正说明单靠时间判定会误删基线，
    真正拦住它的是白名单。所以这里必须同时给出两组数字：删掉的、和没动的。
    """
    _insert_material(ttl_db, "MAT-T-ABC123-1", OLD)
    _insert_material(ttl_db, "MAT-T-ABC123-2", OLD)
    _insert_order(ttl_db, "WO-T-DEF456-1", "FG-TEST-01", OLD)

    before = row_counts(ttl_db)
    assert before["materials"] == 7, f"准备阶段不符合预期：{before}"
    assert before["work_orders"] == 1, f"准备阶段不符合预期：{before}"

    dry = purge(ttl_db, 24.0)
    assert dry.applied is False, "默认必须是试运行"
    assert row_counts(ttl_db) == before, "试运行阶段不允许改动数据"
    assert dry.planned_rows > 0, f"试运行应当算出待删行数，实际 {dry.planned_rows}"

    report = purge(ttl_db, 24.0, apply=True)
    after = row_counts(ttl_db)

    assert after["materials"] == 5, f"过期测试物料没删干净：{before} -> {after}"
    assert after["work_orders"] == 0, f"过期测试工单没删干净：{before} -> {after}"
    assert after["production_batches"] == 0, f"工单的子批次没跟着删（会留孤儿或撞外键）：{after}"
    assert after["stock_ledger"] == 0, f"过期工单的流水没跟着删：{after}"

    # 基线：白名单护住的三类行数必须一分不动。
    assert after["users"] == 5, f"基线用户被误删：{after}"
    assert after["materials"] == len(SEED_MATERIALS), f"基线物料被误删：{after}"
    assert after["stock"] == len(SEED_STOCK), f"基线库存被误删：{after}"
    assert report.baseline_kept == {"users": 5, "materials": 5, "stock": 5}


def test_expired_baseline_rows_survive_even_with_zero_ttl(ttl_db: Path) -> None:
    """TTL 设成 0（字面意义上的"什么都算过期"）时，基线数据依然必须活着。

    这是白名单作用的极限测试：如果判据里只有时间，这条用例会把整库种子删光，
    之后所有用例一起红，而且没人会想到是清理脚本干的。
    """
    report = purge(ttl_db, 0.0, apply=True)
    after = row_counts(ttl_db)

    assert after["users"] == 5, f"TTL=0 把基线用户删了：{after}"
    assert after["materials"] == len(SEED_MATERIALS), f"TTL=0 把基线物料删了：{after}"
    assert after["stock"] == len(SEED_STOCK), f"TTL=0 把基线库存删了：{after}"
    assert report.items == [], f"TTL=0 也不该把基线当成待删项：{report.items}"


def test_purge_is_idempotent(ttl_db: Path) -> None:
    """连跑两次：第二次不报错、不误删、没有可删项 —— 行数逐表一致。"""
    _insert_material(ttl_db, "MAT-T-ABC123-1", OLD)
    _insert_order(ttl_db, "WO-T-DEF456-1", "FG-TEST-01", OLD)

    first = purge(ttl_db, 24.0, apply=True)
    after_first = row_counts(ttl_db)
    assert first.planned_rows > 0, "第一次应当有可删项"

    second = purge(ttl_db, 24.0, apply=True)
    after_second = row_counts(ttl_db)

    assert second.items == [], f"第二次仍有可删项，说明清理没删干净或不幂等：{second.items}"
    assert after_second == after_first, f"第二次跑动了数据：{after_first} -> {after_second}"
    assert second.lingering == [] or all("残留" not in row for row in second.lingering)


def test_ambiguous_expired_rows_are_reported_not_deleted(ttl_db: Path) -> None:
    """ "已过期但命名看不出是测试数据"的行：保留 + 报出来，绝不猜着删。

    真实场景：开发者手工造的演示数据。它有业务价值，删掉就是事故；
    但它确实在漂移基线。所以本模块的选择是"既不删也不瞒" —— 报给使用者处置。
    """
    _insert_order(ttl_db, "WO-SMOKE-001", "FG-TEST-01", OLD)

    report = purge(ttl_db, 24.0, apply=True)

    assert report.items == [], f"命名认不出来的行不该进删除集合：{report.items}"
    assert row_counts(ttl_db)["work_orders"] == 1, "命名认不出来的行被删掉了"
    assert any("WO-SMOKE-001" in row for row in report.unidentified), (
        f"被保留的行必须报出来，否则问题只是换了个地方藏：{report.unidentified}"
    )
    assert "WO-SMOKE-001" in "\n".join(lingering_rows(ttl_db)), "残留清单里应当能看到它"


# ---------- 4&5 的判据本身 ----------
def test_naming_rule_separates_test_data_from_baseline() -> None:
    """区分"测试数据"与"基线数据"的两条规则各自成立。

    规则一（正）：工厂生成的 `前缀-6位随机-序号` 形状可识别 —— 这是可自动清理的部分。
    规则二（反）：种子编码与基线账号**永远**认不出来，所以永远进不了删除集合。
    两条一起才叫"可靠区分"：只有正规则会误删基线，只有反规则则什么都清不掉。
    """
    assert is_test_code("WO-T-ABC123-1"), "工厂生成的工单号必须能识别"
    assert is_test_code("MAT-T-ABC123-12"), "工厂生成的物料编码必须能识别"
    assert is_test_code("WO-LEDGER-ABC123-3"), "用例自定前缀 + 工厂随机后缀也要能识别"
    assert is_test_code("WO-IDEM-ABC123"), "已登记的手写测试单号必须能识别"
    assert is_test_code("MAT-ACL"), "权限用例真的建出来的那个物料必须能识别"
    assert is_test_code("FG-TEST-01"), "成品编码是测试用的，必须能识别"
    assert is_test_code("FG-X"), "精度用例里的成品编码也是测试数据"
    assert is_test_code("FG-NOREPLAY"), "重放用例里的成品编码也是测试数据"

    for baseline in SEED_MATERIALS:
        assert not is_test_code(baseline), f"基线物料 {baseline} 被误判成测试数据"
    assert not is_test_code(None)
    assert not is_test_code("")
    assert not is_test_code("WO-SMOKE-001"), "识别不出来的宁可不认，也不能靠猜"
    assert BASELINE_MATERIALS == frozenset(SEED_MATERIALS)
    assert BASELINE_USERS == frozenset(SEED_USERS)
    assert BASELINE_STOCK == frozenset(SEED_STOCK)


def test_timestamp_parsing_accepts_both_real_formats() -> None:
    """时间戳有两种真实形状，解析必须都认，且口径一致。

    服务端用 `now_iso()` 写的是带 +08:00 的 ISO 串；而 `force_status()` 里的
    SQL `datetime('now')` 写的是 **UTC、无偏移**的串。把后者当东八区会整整偏 8 小时，
    结果是"该清的没清、不该清的清了" —— 两边都是错的。
    """
    with_offset = parse_ts("2026-02-14T10:23:45+08:00")
    naive_utc = parse_ts("2026-02-14T02:23:45")

    assert with_offset is not None, "带偏移的时间戳必须解析成功"
    assert naive_utc is not None, "无偏移的时间戳必须解析成功"
    assert with_offset == naive_utc, "同一时刻的两种写法必须解析成同一个瞬间"
    assert parse_ts("") is None
    assert parse_ts(None) is None
    assert parse_ts("不是时间") is None, "解析不出来的值必须返回 None（按未过期处理，不猜着删）"


def test_verify_clean_reports_residue_when_cleanup_did_not_run(ttl_db: Path) -> None:
    """回收没跑（或没生效）时，`verify_clean()` 必须报出残留。

    这是兜底链条的最后一环：没有它，一次没生效的 DELETE 会表现为
    "用例全绿、下一轮基线漂移"，报错信息指向下一轮，跟真正原因隔了一层。
    """
    _insert_order(ttl_db, "WO-T-DEF456-1", "FG-TEST-01", OLD)
    factory = DataFactory(mes=None, server_root="http://127.0.0.1:1", db_path=ttl_db)
    factory.register_order("WO-T-DEF456-1")

    residues = factory.verify_clean()

    assert residues, "库里明明还有残留，复核却没报出来"
    assert any("WO-T-DEF456-1" in row for row in residues), f"复核没点明是哪个单号：{residues}"
