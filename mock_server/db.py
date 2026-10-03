"""SQLite 存储层：建表、连接管理、事务、种子数据。

为什么用 SQLite 而不是内存 dict 或 SQLAlchemy：
- 用**真实数据库**才能造出真实约束：唯一索引、外键、CHECK 约束、行锁、
  BEGIN IMMEDIATE 事务。写用例时"服务端真的会返回 4xx"是因为约束真的在数据库上。
- 不引 SQLAlchemy：依赖越少，别人 clone 下来越快跑起来；而且这里刻意想让
  "约束写在哪"一眼可见（DDL 就是契约）。

并发处理要点：
- 每个线程一个连接（threading.local）。SQLite 连接不能跨线程共用。
- WAL 模式：读不阻塞写，写不阻塞读。
- 写操作一律 `BEGIN IMMEDIATE`：一开始就拿写锁，避免"先读后写"时
  两个请求都读到 30 库存、都扣 20、最后变成 -10（经典超卖）。
- busy_timeout 给足 10 秒：让并发请求串行排队，而不是直接抛 database is locked。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from mock_server.errors import BizError

DEFAULT_DB_NAME = "dsh_mes.db"

_local = threading.local()

# 全局数据库路径（跨线程可见，见 configure() 的注释）
_DB_PATH: Path | None = None

SCHEMA_SQL = """
-- ============ 用户与角色 ============
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL
                  CHECK (role IN ('ADMIN', 'PLANNER', 'OPERATOR', 'QA')),
    display_name  TEXT    NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at    TEXT    NOT NULL
);

-- ============ 物料主数据 ============
CREATE TABLE IF NOT EXISTS materials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material_code   TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    unit            TEXT    NOT NULL,
    unit_price_cent INTEGER NOT NULL CHECK (unit_price_cent >= 0),
    safety_stock_milli INTEGER NOT NULL DEFAULT 0 CHECK (safety_stock_milli >= 0),
    status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'INACTIVE')),
    -- 软删除：deleted_at 非空即不可见。真实系统极少物理删除主数据，
    -- 因为历史单据还要靠它做追溯。
    deleted_at      TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_materials_status ON materials(status, deleted_at);

-- ============ 生产工单 ============
CREATE TABLE IF NOT EXISTS work_orders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no               TEXT    NOT NULL UNIQUE,
    product_code           TEXT    NOT NULL,
    product_name           TEXT    NOT NULL,
    planned_qty_milli      INTEGER NOT NULL CHECK (planned_qty_milli > 0),
    reported_good_milli    INTEGER NOT NULL DEFAULT 0 CHECK (reported_good_milli >= 0),
    reported_scrap_milli   INTEGER NOT NULL DEFAULT 0 CHECK (reported_scrap_milli >= 0),
    stocked_in_qty_milli   INTEGER NOT NULL DEFAULT 0 CHECK (stocked_in_qty_milli >= 0),
    status                 TEXT    NOT NULL DEFAULT 'PENDING'
                           CHECK (status IN ('PENDING', 'APPROVED', 'SCHEDULED',
                                             'IN_PROGRESS', 'COMPLETED', 'SHIPPED',
                                             'CANCELLED')),
    priority               TEXT    NOT NULL DEFAULT 'NORMAL'
                           CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
    owner                  TEXT    NOT NULL DEFAULT '',
    reviewer               TEXT,
    qc_result              TEXT    NOT NULL DEFAULT 'NONE'
                           CHECK (qc_result IN ('NONE', 'PASS', 'FAIL', 'WAIVED')),
    qc_note                TEXT,
    -- 乐观锁版本号：任何状态变更都让 version+1。
    -- 客户端提交时必须回传它读到的 version，不匹配即 409。
    version                INTEGER NOT NULL DEFAULT 1,
    idempotency_key        TEXT    UNIQUE,
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status, id);
CREATE INDEX IF NOT EXISTS idx_wo_product ON work_orders(product_code);

-- ============ 生产批次 ============
CREATE TABLE IF NOT EXISTS production_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no          TEXT    NOT NULL UNIQUE,
    work_order_id     INTEGER NOT NULL REFERENCES work_orders(id),
    planned_qty_milli INTEGER NOT NULL CHECK (planned_qty_milli > 0),
    good_qty_milli    INTEGER NOT NULL DEFAULT 0 CHECK (good_qty_milli >= 0),
    scrap_qty_milli   INTEGER NOT NULL DEFAULT 0 CHECK (scrap_qty_milli >= 0),
    status            TEXT    NOT NULL DEFAULT 'CREATED'
                      CHECK (status IN ('CREATED', 'IN_PROGRESS', 'CLOSED')),
    line              TEXT    NOT NULL,
    operator          TEXT    NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_wo ON production_batches(work_order_id);

-- ============ 库存余额（一物料一仓一行）============
CREATE TABLE IF NOT EXISTS stock (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material_code   TEXT    NOT NULL,
    warehouse       TEXT    NOT NULL,
    -- CHECK 是最后一道防线：就算应用层逻辑写错，数据库也不会让库存变负。
    -- 这种"多一层兜底"是真实系统的常见做法（应用校验 + 数据库约束双保险）。
    available_milli INTEGER NOT NULL DEFAULT 0 CHECK (available_milli >= 0),
    reserved_milli  INTEGER NOT NULL DEFAULT 0 CHECK (reserved_milli >= 0),
    issued_milli    INTEGER NOT NULL DEFAULT 0 CHECK (issued_milli >= 0),
    updated_at      TEXT    NOT NULL,
    UNIQUE (material_code, warehouse)
);

-- ============ 出入库流水（只增不改，审计与追溯的真相来源）============
CREATE TABLE IF NOT EXISTS stock_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    material_code TEXT    NOT NULL,
    warehouse     TEXT    NOT NULL,
    txn_type      TEXT    NOT NULL
                  CHECK (txn_type IN ('RECEIPT', 'ISSUE', 'REVERSE')),
    qty_milli     INTEGER NOT NULL CHECK (qty_milli > 0),
    balance_milli INTEGER NOT NULL CHECK (balance_milli >= 0),
    work_order_no TEXT,
    batch_no      TEXT,
    operator      TEXT    NOT NULL,
    remark        TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_material ON stock_ledger(material_code, id);

-- ============ 质检记录 ============
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id INTEGER NOT NULL REFERENCES work_orders(id),
    result        TEXT    NOT NULL CHECK (result IN ('PASS', 'FAIL', 'WAIVED')),
    inspect_qty_milli INTEGER NOT NULL CHECK (inspect_qty_milli > 0),
    defect_qty_milli  INTEGER NOT NULL DEFAULT 0 CHECK (defect_qty_milli >= 0),
    note          TEXT,
    inspector     TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspection_wo ON inspections(work_order_id);

-- ============ 幂等键 ============
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key      TEXT    NOT NULL,
    scope         TEXT    NOT NULL,
    request_hash  TEXT    NOT NULL,
    state         TEXT    NOT NULL DEFAULT 'IN_PROGRESS'
                  CHECK (state IN ('IN_PROGRESS', 'DONE')),
    response_code INTEGER,
    response_body TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    -- 幂等的核心就在这个索引上：同一个键 + 同一个接口只能存在一条记录。
    -- 重复提交时插入会撞唯一索引，服务端据此知道"这是一次重放"。
    UNIQUE (idem_key, scope)
);
"""

SEED_USERS = [
    # (username, password, role, display_name)
    ("admin", "admin123", "ADMIN", "系统管理员"),
    ("planner01", "plan123", "PLANNER", "计划员-张工"),
    ("operator01", "op123", "OPERATOR", "产线操作员-李工"),
    ("operator02", "op123", "OPERATOR", "产线操作员-王工"),
    ("qa01", "qa123", "QA", "质检员-陈工"),
]

SEED_MATERIALS = [
    # (code, name, category, unit, unit_price 元, safety_stock，库存, 仓库)
    ("MAT-CU-001", "紫铜板 T2 1.0mm", "原材料", "KG", "62.50", "0.000", "120.000", "WH-01"),
    ("MAT-AL-002", "铝合金板 6061 2.0mm", "原材料", "KG", "28.00", "0.000", "80.000", "WH-01"),
    ("MAT-SC-003", "十字槽盘头螺钉 M3x8", "标准件", "PCS", "0.03", "0.000", "3000.000", "WH-01"),
    ("MAT-PCB-004", "PCB 主控板 REV.B", "电子料", "PCS", "180.00", "0.000", "50.000", "WH-02"),
    ("MAT-ENC-005", "铝合金外壳 6063", "结构件", "PCS", "45.00", "0.000", "20.000", "WH-02"),
]


def get_default_db_path() -> Path:
    return Path(__file__).resolve().parent / DEFAULT_DB_NAME


# ---------- 连接管理 ----------
def _new_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def configure(db_path: str | Path) -> None:
    """指定当前进程使用的数据库文件。测试会指向临时文件。

    为什么用**模块级全局** + thread-local 双重记录：
    uvicorn 处理请求用的是线程池，而 threading.local 在另一个线程里读不到，
    于是 `configure()` 设的路径在请求线程里会"消失"，服务悄悄连回默认库。
    这个 bug 的可怕之处在于它不报错——测试会在错误的库上跑并且全绿。
    全局变量保证跨线程可见；thread-local 用来缓存每个线程的连接与它所属的路径。
    """
    global _DB_PATH
    _DB_PATH = Path(db_path)
    _local.db_path = _DB_PATH


def db_path() -> Path:
    path = getattr(_local, "db_path", None)
    if path is None:
        path = _DB_PATH if _DB_PATH is not None else get_default_db_path()
        _local.db_path = path
    return path


def get_connection() -> sqlite3.Connection:
    path = db_path()
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "conn_path", None) != path:
        if conn is not None:
            conn.close()
        conn = _new_connection(path)
        _local.conn = conn
        _local.conn_path = path
    return conn


def close_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.conn_path = None


@contextmanager
def read_tx():
    """只读事务：默认 BEGIN（DEFERRED），不拿写锁，读并发不受影响。"""
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def write_tx():
    """写事务：BEGIN IMMEDIATE，开头就拿写锁。

    这是"不超卖"的关键。如果用 DEFERRED，两个并发请求会同时读到
    库存 120，各自判断"够扣"，然后一个 COMMIT 成功、另一个在 COMMIT 时才
    报锁冲突——此时业务判断已经做完了，只能整个回滚重来。
    IMMEDIATE 让第二个请求在 BEGIN 就排队，读到的一定是最新余额。
    """
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def healthcheck() -> dict:
    with read_tx() as conn:
        tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {"database": str(db_path()), "tables": tables}


# ---------- 初始化 ----------
def init_db(reset: bool = False) -> Path:
    """建表 + 写入种子数据。reset=True 会先删库（测试用）。"""
    from mock_server.security import hash_password
    from mock_server.util import now_iso

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if reset:
        close_connection()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(path) + suffix)
            if f.exists():
                f.unlink()

    conn = _new_connection(path)
    try:
        # 连接是 autocommit（isolation_level=None），DDL/DML 各自立即生效。
        # 注意：`executescript` 会先隐式 COMMIT 再执行脚本，所以这里**不能**
        # 手工再 BEGIN/COMMIT——否则会撞 "cannot commit - no transaction is active"。
        # 初始化只在服务启动时跑一次，并且服务还没开始接请求，
        # 因此不需要把它包成一个事务。
        conn.executescript(SCHEMA_SQL)
        now = now_iso()

        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO users (username, password_hash, role, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
                [(u, hash_password(p), r, d, now) for u, p, r, d in SEED_USERS],
            )

        if conn.execute("SELECT COUNT(*) AS c FROM materials").fetchone()["c"] == 0:
            from mock_server.quantity import parse_cent, parse_qty, parse_qty_allow_zero

            for code, name, cat, unit, price, safety, qty, wh in SEED_MATERIALS:
                conn.execute(
                    "INSERT INTO materials (material_code, name, category, unit,"
                    " unit_price_cent, safety_stock_milli, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    # 安全库存允许为 0（不是所有物料都要设安全库存），
                    # 所以这里用 parse_qty_allow_zero 而不是 parse_qty。
                    (code, name, cat, unit, parse_cent(price), parse_qty_allow_zero(safety, "safety_stock"), now, now),
                )
                conn.execute(
                    "INSERT INTO stock (material_code, warehouse, available_milli, updated_at) VALUES (?, ?, ?, ?)",
                    (code, wh, parse_qty(qty), now),
                )
    finally:
        conn.close()
    return path


def ensure_ready() -> None:
    """服务启动时调用：库不存在就建，存在就用。"""
    path = db_path()
    if not path.exists():
        init_db()
    else:
        with read_tx() as conn:
            exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if not exists:
            init_db()


def table_counts() -> dict[str, int]:
    """各表行数快照。用例隔离校验用：一次用例跑完，行数必须回到基线。"""
    tables = (
        "users",
        "materials",
        "work_orders",
        "production_batches",
        "stock",
        "stock_ledger",
        "inspections",
        "idempotency_keys",
    )
    counts: dict[str, int] = {}
    with read_tx() as conn:
        for t in tables:
            counts[t] = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
    return counts


__all__ = [
    "BizError",
    "configure",
    "db_path",
    "ensure_ready",
    "get_connection",
    "get_default_db_path",
    "healthcheck",
    "init_db",
    "read_tx",
    "rows_to_dicts",
    "table_counts",
    "write_tx",
]
