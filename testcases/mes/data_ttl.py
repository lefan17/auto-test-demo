"""测试数据的 TTL 清理：把「过期残留」与「基线数据」可靠地分开。

为什么要单独一层（它补的是哪一段）：
`data_factory` 的定向回收与 conftest 的会话级隔离断言都只在**进程还活着**的
前提下有效。会话一旦被 Ctrl-C / 崩溃 / CI 超时打断，teardown 根本没有机会跑，
残留就留在库里；而下次跑的是**另一个库**（session fixture 每次都 mktemp），
于是没人会发现。本模块就是给这种「跨会话残留」准备的按时间清理入口。

判据（测试数据 vs 基线数据）——两级，基线白名单是不可越过的硬否决：
1. 【基线白名单】`SEED_USERS` 的账号名、`SEED_MATERIALS` 的物料编码、
   `SEED_STOCK` 的 (物料, 仓库) 键。这三份名单与 `mock_server/db.py` 的种子同源，
   也与会话级隔离断言的口径同源；**命中即永不删除，哪怕它已经"过期"**
   —— 种子行的 created_at 就是建库那一刻，本来就老于任何 TTL，
   这正是"只看时间判定"会闯祸的地方。
   其余 5 张表（work_orders / production_batches / inspections / stock_ledger /
   idempotency_keys）的基线是 0 行，所以那 5 张表里**非空即残留**。
2. 【过期判定】`created_at` 早于 `now - N 小时`（N 由调用方给，见 CLI 的 --ttl-hours）。
   只有「过期 **且** 不在白名单」才进删除集合。单靠命名不够：仓库里有一批
   用例**手写**的测试编码不含工厂随机标记（见 KNOWN_TEST_CODES / KNOWN_TEST_PREFIXES），
   正则认不出来，只能显式登记。

级联：工单过期时它的批次/质检/流水按**父行**判定一起删；物料过期时它的库存行
按物料编码一起删。子行不按自身时间判定 —— 父子时间不一致时会撞外键或留下孤儿。

`users` 表只报告不自动删：当前没有任何用例会建账号（`apis/` 与 `testcases/mes/`
全库搜 `/users` 零命中），多出来的账号一定出自人工操作，删它的风险大于收益。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_factory import SEED_MATERIALS, SEED_STOCK, SEED_USERS
from data_isolation import row_counts

DEFAULT_TTL_HOURS = 24.0
CN_TZ = timezone(timedelta(hours=8))

# 工厂生成的编码形状：`{前缀}-{6 位随机}-{自增序号}`（见 DataFactory.next/tag）。
# 前缀有多少段都认，所以 MAT-T-ABC123-1 与 WO-LEDGER-ABC123-7 都命中。
FACTORY_TAG_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[A-Z0-9]{6}-\d+$")

# 用例里**手写**的测试编码：不含工厂随机标记，正则认不出来，只能显式登记。
KNOWN_TEST_CODES = frozenset({"FG-TEST-01", "FG-X", "FG-NOREPLAY", "MAT-ACL"})
KNOWN_TEST_PREFIXES = ("WO-IDEM-", "WO-CONFLICT-", "WO-DUP-", "WO-FIXED-", "WO-LONGKEY-")

# 基线白名单：与 mock_server/db.py 的种子同源（经 data_factory 转发）。
BASELINE_USERS = frozenset(SEED_USERS)
BASELINE_MATERIALS = frozenset(SEED_MATERIALS)
BASELINE_STOCK = frozenset(SEED_STOCK)


def is_test_code(code: str | None) -> bool:
    """这个编码看得出是测试造的吗（工厂随机标记 / 已登记的手写字面量）。"""
    if not code:
        return False
    return bool(FACTORY_TAG_RE.match(code)) or code in KNOWN_TEST_CODES or code.startswith(KNOWN_TEST_PREFIXES)


def parse_ts(value: object) -> datetime | None:
    """解析库里的时间戳，容错两种**真实存在**的形状。

    - 服务端 `mock_server.util.now_iso()`：`2026-02-14T10:23:45+08:00`（带偏移、秒精度）
    - `data_factory.force_status()` 里的 SQL `datetime('now')`：`2026-02-14T02:23:45`（UTC、无偏移）

    无偏移的一律按 **UTC** 解释：SQLite 的 `datetime('now')` 就是 UTC，
    当成东八区会让判断整整偏 8 小时（该清的没清、不该清的清了）。
    """
    if value in (None, ""):
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _expired(value: object, cutoff: datetime) -> bool:
    stamp = parse_ts(value)
    # 解析不出来的行当"未过期"：宁可留着被人看到，也不猜着删。
    return stamp is not None and stamp < cutoff


@dataclass
class PurgeTargets:
    """一次清理要动的行（rowid 粒度）。

    `_plan` 与 `_apply` 共用同一份，避免"报告说删 3 行、实际删了 5 行"这类不一致。
    """

    order_ids: list[int] = field(default_factory=list)
    order_nos: list[str] = field(default_factory=list)
    product_codes: set[str] = field(default_factory=set)
    material_codes: list[str] = field(default_factory=list)
    stock_ids: list[int] = field(default_factory=list)
    ledger_ids: list[int] = field(default_factory=list)
    idem_ids: list[int] = field(default_factory=list)
    batch_ids: list[int] = field(default_factory=list)
    inspection_ids: list[int] = field(default_factory=list)


@dataclass
class PurgeItem:
    """一条删除（或计划删除）项：够人读懂，也够断言用。"""

    table: str
    key: str
    rows: int
    reason: str

    def __str__(self) -> str:
        return f"{self.table}: {self.key} —— {self.rows} 行（{self.reason}）"


@dataclass
class PurgeReport:
    db_path: Path
    ttl_hours: float
    cutoff: datetime
    applied: bool
    items: list[PurgeItem] = field(default_factory=list)
    baseline_kept: dict[str, int] = field(default_factory=dict)
    baseline_lost: dict[str, tuple[int, int]] = field(default_factory=dict)
    lingering: list[str] = field(default_factory=list)
    unidentified: list[str] = field(default_factory=list)
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    @property
    def planned_rows(self) -> int:
        return sum(item.rows for item in self.items)

    def as_dict(self) -> dict:
        return {
            "db": str(self.db_path),
            "ttl_hours": self.ttl_hours,
            "cutoff": self.cutoff.isoformat(timespec="seconds"),
            "applied": self.applied,
            "planned_rows": self.planned_rows,
            "items": [{"table": i.table, "key": i.key, "rows": i.rows, "reason": i.reason} for i in self.items],
            "baseline_kept": self.baseline_kept,
            "baseline_lost": {table: list(pair) for table, pair in self.baseline_lost.items()},
            "lingering": self.lingering,
            "unidentified": self.unidentified,
            "row_counts_before": self.before,
            "row_counts_after": self.after,
        }


def connect(db_path: Path) -> sqlite3.Connection:
    """清理用的连接口径与服务端保持一致：外键真的开、忙等 10s。"""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ids(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[int]:
    return [int(row[0]) for row in conn.execute(sql, params).fetchall()]


def _placeholders(values) -> str:
    return ",".join("?" * len(values))


def _collect(conn: sqlite3.Connection, cutoff: datetime) -> tuple[PurgeTargets, list[str]]:
    """算出「过期且非基线」的完整删除集合。纯读，不动数据。

    返回 (删除集合, 保留清单)。保留清单装的是「已过期、但对不上任何测试命名规则」的行：
    它们可能是人工造的演示数据，删掉的风险大于收益，所以既不删也不瞒 —— 报出来让人处置。
    这是本模块"宁可留着被看见，也不猜着删"原则的落点。
    """
    targets = PurgeTargets()
    kept: list[str] = []

    orders = conn.execute("SELECT id, order_no, product_code, created_at FROM work_orders").fetchall()
    all_order_nos = {str(row[1]) for row in orders}
    for row in orders:
        if not _expired(row[3], cutoff):
            continue
        if is_test_code(str(row[1])):
            targets.order_ids.append(int(row[0]))
            targets.order_nos.append(str(row[1]))
            # 成品入库不建 materials 行，只按 product_code 往 stock 里插一行；
            # 工单要删，那行库存就得跟着走。只收**真要删的**工单的 product_code，
            # 否则"保留清单"里的工单会被掏空库存却留着工单本身。
            if row[2]:
                targets.product_codes.add(str(row[2]))
        else:
            kept.append(f"work_orders: {row[1]}（created_at={row[3]}，单号看不出是测试数据）")

    for code, created_at in conn.execute("SELECT material_code, created_at FROM materials ORDER BY id").fetchall():
        if code in BASELINE_MATERIALS or not _expired(created_at, cutoff):
            continue
        if is_test_code(str(code)):
            targets.material_codes.append(str(code))
        else:
            kept.append(f"materials: {code}（created_at={created_at}，编码看不出是测试数据）")
    purge_codes = targets.product_codes | set(targets.material_codes)

    if targets.order_ids:
        ph = _placeholders(targets.order_ids)
        # 批次/质检按**父行**判定，不看自身时间：父子时间不一致时按自身时间判定
        # 要么撞外键，要么留下没有父行的孤儿。
        targets.batch_ids = _ids(
            conn, f"SELECT id FROM production_batches WHERE work_order_id IN ({ph})", tuple(targets.order_ids)
        )
        targets.inspection_ids = _ids(
            conn, f"SELECT id FROM inspections WHERE work_order_id IN ({ph})", tuple(targets.order_ids)
        )

    # 流水按**归属**判定，理由和子行一样：它是审计轨迹（只增不改），
    # 删掉一条保留数据的流水，等于悄悄改写了一份我们刚刚决定不动它的账。
    # 只有"归属的工单/物料确实在删"或"本来就没有归属（孤儿）且自身过期"才删。
    target_order_nos = set(targets.order_nos)
    ledger_rows = conn.execute("SELECT id, material_code, work_order_no, created_at FROM stock_ledger").fetchall()
    for row in ledger_rows:
        ledger_id, material_code, owner, created_at = int(row[0]), str(row[1]), row[2], row[3]
        owner_no = None if owner is None else str(owner)
        if owner_no is not None and owner_no in target_order_nos:
            targets.ledger_ids.append(ledger_id)
        elif material_code in purge_codes:
            targets.ledger_ids.append(ledger_id)
        elif not _expired(created_at, cutoff):
            continue
        elif owner_no and owner_no in all_order_nos:
            kept.append(f"stock_ledger #{ledger_id}（归属 {owner_no}，那张工单被保留，不单独动它的账）")
        else:
            # 自身过期且没有归属：没人认领的历史流水，清掉才不会污染基线。
            targets.ledger_ids.append(ledger_id)
    targets.ledger_ids = sorted(set(targets.ledger_ids))

    # 幂等键是**唯一**按自身时间判定的表：它生来就带有效期语义（过期即失效本来
    # 就是它的正常结局），且基线 0 行，没有"归属"可供对照。
    targets.idem_ids = [
        int(row[0])
        for row in conn.execute("SELECT id, created_at FROM idempotency_keys").fetchall()
        if _expired(row[1], cutoff)
    ]

    # 库存行按 (物料, 仓库) 键保护：种子库存即使编码落进 purge_codes 也不动。
    targets.stock_ids = [
        int(row[0])
        for row in conn.execute("SELECT id, material_code, warehouse FROM stock").fetchall()
        if (str(row[1]), str(row[2])) not in BASELINE_STOCK and str(row[1]) in purge_codes
    ]
    return targets, kept


def baseline_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """白名单那几行**现在还在不在**。

    注意它数的是"该活着的行"，不是"表里总共有多少行" —— 后者没用：
    清理前总行数本来就包含待删的测试数据。只有"基线行数守恒"才说明没误删。
    """
    users = conn.execute(
        f"SELECT COUNT(*) FROM users WHERE username IN ({_placeholders(BASELINE_USERS)})",
        tuple(BASELINE_USERS),
    ).fetchone()[0]
    materials = conn.execute(
        f"SELECT COUNT(*) FROM materials WHERE material_code IN ({_placeholders(BASELINE_MATERIALS)})",
        tuple(BASELINE_MATERIALS),
    ).fetchone()[0]
    # 库存要用 (物料, 仓库) 这两个列做匹配：同一个物料编码在另一个仓库的行不算基线。
    pair_clause = " OR ".join("(material_code = ? AND warehouse = ?)" for _ in BASELINE_STOCK)
    pair_params = tuple(value for pair in sorted(BASELINE_STOCK) for value in pair)
    stock = conn.execute(f"SELECT COUNT(*) FROM stock WHERE {pair_clause}", pair_params).fetchone()[0]
    return {"users": int(users), "materials": int(materials), "stock": int(stock)}


def _plan(targets: PurgeTargets) -> list[PurgeItem]:
    items: list[PurgeItem] = []
    if targets.order_ids:
        items.append(PurgeItem("work_orders", "、".join(targets.order_nos), len(targets.order_ids), "工单已过期"))
    if targets.batch_ids:
        items.append(
            PurgeItem("production_batches", f"{len(targets.batch_ids)} 行", len(targets.batch_ids), "所属工单已过期")
        )
    if targets.inspection_ids:
        items.append(
            PurgeItem("inspections", f"{len(targets.inspection_ids)} 行", len(targets.inspection_ids), "所属工单已过期")
        )
    if targets.ledger_ids:
        items.append(
            PurgeItem(
                "stock_ledger",
                f"{len(targets.ledger_ids)} 条流水",
                len(targets.ledger_ids),
                "自身过期或归属已过期的工单/物料（该表基线 0 行）",
            )
        )
    if targets.idem_ids:
        items.append(
            PurgeItem(
                "idempotency_keys",
                f"{len(targets.idem_ids)} 条幂等键",
                len(targets.idem_ids),
                "幂等键已过期（该表基线 0 行）",
            )
        )
    if targets.stock_ids:
        items.append(
            PurgeItem(
                "stock",
                f"{len(targets.stock_ids)} 行",
                len(targets.stock_ids),
                "所属物料/工单已过期；种子库存按 (物料, 仓库) 键受保护",
            )
        )
    if targets.material_codes:
        items.append(
            PurgeItem(
                "materials", "、".join(targets.material_codes), len(targets.material_codes), "物料已过期且不在种子名单"
            )
        )
    return items


def _apply(conn: sqlite3.Connection, targets: PurgeTargets) -> None:
    """按依赖倒序删掉 targets 里的行。顺序反了会撞外键（PRAGMA foreign_keys=ON 是真的生效的）。"""
    if targets.ledger_ids:
        conn.execute(
            f"DELETE FROM stock_ledger WHERE id IN ({_placeholders(targets.ledger_ids)})", tuple(targets.ledger_ids)
        )
    if targets.inspection_ids:
        conn.execute(
            f"DELETE FROM inspections WHERE id IN ({_placeholders(targets.inspection_ids)})",
            tuple(targets.inspection_ids),
        )
    if targets.batch_ids:
        conn.execute(
            f"DELETE FROM production_batches WHERE id IN ({_placeholders(targets.batch_ids)})",
            tuple(targets.batch_ids),
        )
    if targets.idem_ids:
        conn.execute(
            f"DELETE FROM idempotency_keys WHERE id IN ({_placeholders(targets.idem_ids)})", tuple(targets.idem_ids)
        )
    if targets.order_ids:
        conn.execute(
            f"DELETE FROM work_orders WHERE id IN ({_placeholders(targets.order_ids)})", tuple(targets.order_ids)
        )
    if targets.stock_ids:
        conn.execute(f"DELETE FROM stock WHERE id IN ({_placeholders(targets.stock_ids)})", tuple(targets.stock_ids))
    if targets.material_codes:
        conn.execute(
            f"DELETE FROM materials WHERE material_code IN ({_placeholders(targets.material_codes)})",
            tuple(targets.material_codes),
        )


def lingering_rows(db_path: Path) -> list[str]:
    """列出**非基线**行（不看时间）。

    这是「清理不彻底」的原始信号，与会话级 `data_isolation_check` 的
    `assert before == after` 看的是同一批事实 —— 所以它非空时，
    那条断言必然也会红。
    """
    problems: list[str] = []
    conn = connect(db_path)
    try:
        for username, created_at in conn.execute("SELECT username, created_at FROM users ORDER BY id").fetchall():
            if username not in BASELINE_USERS:
                problems.append(f"  users: 多出账号 {username}（created_at={created_at}）—— 该表只报告不自动删")
        for code, created_at in conn.execute("SELECT material_code, created_at FROM materials ORDER BY id").fetchall():
            if code not in BASELINE_MATERIALS:
                problems.append(f"  materials: 多出物料 {code}（created_at={created_at}）")
        for code, warehouse in conn.execute("SELECT material_code, warehouse FROM stock ORDER BY id").fetchall():
            if (str(code), str(warehouse)) not in BASELINE_STOCK:
                problems.append(f"  stock: 残留库存行 {code}/{warehouse}")
        for order_no, created_at in conn.execute("SELECT order_no, created_at FROM work_orders ORDER BY id").fetchall():
            problems.append(f"  work_orders: 残留工单 {order_no}（created_at={created_at}）")
        for table in ("production_batches", "inspections", "stock_ledger", "idempotency_keys"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                problems.append(f"  {table}: 残留 {count} 行（该表基线 0 行）")
    finally:
        conn.close()
    return problems


def purge(
    db_path: Path,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> PurgeReport:
    """清理超过 `ttl_hours` 的历史测试数据。默认 **dry-run**：只算不删。

    幂等：清理只做"删除已存在且已过期的行"，第二次跑时那些行已经不在了，
    于是 items 为空、行数不变，既不报错也不动基线 —— 这就是重复执行的正确形态。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    if ttl_hours < 0:
        raise ValueError(f"ttl_hours 不能为负：{ttl_hours}")

    cutoff = (now or datetime.now(CN_TZ)) - timedelta(hours=ttl_hours)
    before = row_counts(db_path)

    conn = connect(db_path)
    try:
        targets, unidentified = _collect(conn, cutoff)
        items = _plan(targets)
        baseline_before = baseline_row_counts(conn)
        if apply and items:
            try:
                _apply(conn, targets)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        baseline_after = baseline_row_counts(conn)
    finally:
        conn.close()

    lost = {
        table: (count, baseline_after[table])
        for table, count in baseline_before.items()
        if baseline_after[table] != count
    }
    after = row_counts(db_path)
    report = PurgeReport(
        db_path=db_path,
        ttl_hours=ttl_hours,
        cutoff=cutoff,
        applied=apply,
        items=items,
        baseline_kept=baseline_after,
        baseline_lost=lost,
        unidentified=unidentified,
        before=before,
        after=after,
    )
    report.lingering = lingering_rows(db_path)
    return report


def format_report(report: PurgeReport) -> str:
    """给人看的报告。密度优先：一行一项，末尾附行数对照。"""
    lines: list[str] = []
    mode = "已执行删除" if report.applied else "试运行（dry-run，未改动数据）"
    lines.append(f"[数据 TTL] {report.db_path}")
    lines.append(f"  口径：早于 {report.cutoff.isoformat(timespec='seconds')}（TTL {report.ttl_hours:g} 小时）")
    lines.append(f"  模式：{mode}；命中的过期测试数据 {len(report.items)} 项 / {report.planned_rows} 行")
    for item in report.items:
        lines.append(f"    - {item}")
    if not report.items:
        lines.append("    （没有命中的过期测试数据）")
    lines.append(f"  受白名单保护的行数：{report.baseline_kept}")
    if report.baseline_lost:
        # 这行是"误删基线"的自报。出现了就是 bug，报告不能替它遮掩。
        lines.append(f"  !! 基线行数减少了（误删）：{report.baseline_lost}")
    changed = {t: (report.before[t], report.after[t]) for t in report.before if report.before[t] != report.after[t]}
    if changed:
        for table, (b, a) in changed.items():
            lines.append(f"    {table}: {b} -> {a}")
    else:
        lines.append("    行数未变化（清理幂等：没有可删的过期数据）")
    if report.unidentified:
        lines.append(f"  已过期但命名对不上测试规则，**本次保留**（{len(report.unidentified)} 项，人工确认后再处置）：")
        lines.extend(f"    - {row}" for row in report.unidentified)
    if report.lingering:
        # 试运行下列的残留里**包含**上面已命中的过期项（还没删，所以它们当然还在）；
        # 措辞要如实说明，否则会读成"这些不会删"。真正没打算删的是 report.unidentified。
        note = "试运行未删（含上面已命中的过期项）" if not report.applied else "未过期或未确认，本次不删"
        lines.append(f"  非基线残留 {len(report.lingering)} 处（{note}）：")
        lines.extend(report.lingering)
    else:
        lines.append("  非基线残留：无（库已回到基线口径）")
    return "\n".join(lines)


__all__ = [
    "BASELINE_MATERIALS",
    "BASELINE_STOCK",
    "BASELINE_USERS",
    "DEFAULT_TTL_HOURS",
    "FACTORY_TAG_RE",
    "KNOWN_TEST_CODES",
    "KNOWN_TEST_PREFIXES",
    "PurgeItem",
    "PurgeReport",
    "connect",
    "format_report",
    "is_test_code",
    "lingering_rows",
    "parse_ts",
    "purge",
]
