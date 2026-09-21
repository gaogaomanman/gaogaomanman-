"""结果序列化：把驱动返回的原生对象转成 JSON 安全值。

达梦除法会产生 Decimal、日期列为 datetime；SQL Server 可能返回 Decimal/bytes。
FastAPI 默认无法序列化 Decimal/datetime，故统一在此转换。
"""
from __future__ import annotations

import datetime
import decimal
from typing import Any


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        # 保持数值语义（旧 Node 侧走 JSON.stringify 也是数值）
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            return str(value)
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)


def json_safe_row(row: Any) -> list:
    """行转位置数组（保持与旧中间池 rows:[[...]] 一致）。"""
    if isinstance(row, (list, tuple)):
        return [json_safe(v) for v in row]
    # pyodbc.Row 支持迭代与下标
    try:
        return [json_safe(v) for v in row]
    except TypeError:  # pragma: no cover
        return [json_safe(row)]
