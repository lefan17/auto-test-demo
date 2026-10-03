"""数量与金额的精确表示。

这是本项目里最"有业务味道"的一个技术点，也是真实系统最常见的资损来源。

问题：浮点数不能表示十进制小数。`0.1 + 0.2 != 0.3` 在 Python 里是 True。
如果库存、工单数量用 float 存，跑几千次出入库之后就会出现
"库存显示 0.30000000000000004" 或者"明明有 0.3，却判定库存不足"。

做法（也是 ERP/MES 的通用做法）：
- **存储**：一律整数。数量按「千分之一」（milli）存，金额按「分」（cent）存。
- **传输**：一律字符串。JSON 里 `"10.500"`，前端和自己做定点解析，
  避免 JSON number 在浏览器里被解析成 IEEE754 双精度而丢精度。
- **运算**：全程整数加减，只在渲染时做一次 Decimal 格式化。

用例会断言 `"0.100" + "0.200" == "0.300"`，这条断言防的线上事故是
"浮点精度导致库存对不上、对账差异"。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from mock_server.errors import ValidationFailed

# 数量的最小单位：千分之一
QTY_SCALE = 1000
# 金额的最小单位：分
CENT_SCALE = 100

MAX_QTY_MILLI = 10**12  # 10 亿，足够任何真实工单；用于挡住溢出和畸形输入


def qty_to_str(milli: int) -> str:
    """整数（千分之一）-> 定点字符串，固定 3 位小数。"""
    sign = "-" if milli < 0 else ""
    milli = abs(int(milli))
    return f"{sign}{milli // QTY_SCALE}.{milli % QTY_SCALE:03d}"


def parse_qty(value: str | int | float | None, field: str = "quantity") -> int:
    """字符串/数字 -> 整数（千分之一）。超过 3 位小数直接拒绝，而不是悄悄四舍五入。

    为什么超过 3 位要拒绝：静默舍入在真实系统里等于"客户报 1.0005 吨，
    系统记 1.000 吨"，累计起来就是账实不符。宁可报错让人改数据。
    """
    if value is None:
        raise ValidationFailed(f"字段 {field} 不能为空")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed(f"字段 {field} 不是合法数字: {value!r}") from exc

    if dec != dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP):
        raise ValidationFailed(f"字段 {field} 最多支持 3 位小数: {value!r}")

    milli = int((dec * QTY_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(milli) > MAX_QTY_MILLI:
        raise ValidationFailed(f"字段 {field} 超出允许范围: {value!r}")
    if milli <= 0:
        raise ValidationFailed(f"字段 {field} 必须大于 0，实际 {value!r}")
    return milli


def parse_qty_allow_zero(value: str | int | float | None, field: str = "quantity") -> int:
    """同 parse_qty，但允许 0（报工良品/报废数可以为 0）。"""
    if value is None:
        raise ValidationFailed(f"字段 {field} 不能为空")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed(f"字段 {field} 不是合法数字: {value!r}") from exc
    if dec != dec.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP):
        raise ValidationFailed(f"字段 {field} 最多支持 3 位小数: {value!r}")
    milli = int((dec * QTY_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    if abs(milli) > MAX_QTY_MILLI:
        raise ValidationFailed(f"字段 {field} 超出允许范围: {value!r}")
    if milli < 0:
        raise ValidationFailed(f"字段 {field} 不能为负数，实际 {value!r}")
    return milli


def parse_bounded_int(value: str | int | None, field: str, low: int, high: int) -> int:
    """整数范围校验，失败抛 422（请求格式问题，不是业务规则问题）。"""
    if value is None:
        raise ValidationFailed(f"字段 {field} 不能为空")
    try:
        num = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"字段 {field} 必须是整数: {value!r}") from exc
    if str(value).strip() not in {str(num), f"+{num}"}:
        raise ValidationFailed(f"字段 {field} 必须是整数: {value!r}")
    if not low <= num <= high:
        raise ValidationFailed(f"字段 {field} 必须在 [{low}, {high}] 之间，实际 {num}")
    return num


def cent_to_str(cent: int) -> str:
    """整数（分）-> 定点字符串，固定 2 位小数。"""
    sign = "-" if cent < 0 else ""
    cent = abs(int(cent))
    return f"{sign}{cent // CENT_SCALE}.{cent % CENT_SCALE:02d}"


def parse_cent(value: str | int | float | None, field: str = "unit_price") -> int:
    """金额字符串 -> 整数（分）。同样拒绝超过 2 位小数。"""
    if value is None:
        raise ValidationFailed(f"字段 {field} 不能为空")
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed(f"字段 {field} 不是合法金额: {value!r}") from exc
    if dec != dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        raise ValidationFailed(f"字段 {field} 最多支持 2 位小数: {value!r}")
    cent = int((dec * CENT_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    if cent < 0:
        raise ValidationFailed(f"字段 {field} 不能为负: {value!r}")
    return cent


def amount_str(qty_milli: int, unit_price_cent: int) -> str:
    """金额 = 数量 × 单价，用整数算完再格式化，全程不出现 float。

    公式：qty_milli(10^-3) × price_cent(10^-2) / 10^3 = cent
    这里先乘后除，并用整数除法配合四舍五入，避免中间结果落到 float。
    """
    product = qty_milli * unit_price_cent  # 单位：10^-5 元
    cent = (product + QTY_SCALE // 2) // QTY_SCALE
    return cent_to_str(int(cent))
