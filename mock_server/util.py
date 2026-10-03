"""杂项工具：时间、编号生成、稳定哈希。"""

from __future__ import annotations

import hashlib
import json
import random
import string
from datetime import datetime, timedelta, timezone

# 东八区：苏州本地制造企业都用北京时间，日志/单据时间不要用 UTC，
# 否则"当天产量"这类按日期分组的统计会在凌晨 8 点错位。
CN_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(CN_TZ).strftime("%Y%m%d")


def random_suffix(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def next_order_no() -> str:
    return f"WO{today_str()}{random_suffix(4)}"


def next_batch_no() -> str:
    return f"B{today_str()}{random_suffix(4)}"


def next_material_code(prefix: str = "MAT-TEST") -> str:
    return f"{prefix}-{random_suffix(6)}"


def stable_hash(payload) -> str:
    """请求内容的稳定指纹，用于判断"同一个幂等键是否被复用于不同内容"。"""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
