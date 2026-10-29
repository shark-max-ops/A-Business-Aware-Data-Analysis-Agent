"""冻结结果的类型和值；Decimal 不转为浮点数。"""

import math
from datetime import date, datetime, time
from decimal import Decimal

from metric_pilot.data.common import DataError


def encode_cell(value):
    """
    作用：编码受支持的 DuckDB 标量
    输入：数据库值
    输出：保留 null、数值和十进制精度的 JSON 值
    """
    if type(value) is int and abs(value) > 2**53 - 1:
        return {"type": "integer", "value": str(value)}
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataError("NONFINITE_RESULT")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DataError("NONFINITE_RESULT")
        return {"type": "decimal", "value": format(value, "f")}
    if isinstance(value, (datetime, date, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    raise DataError("UNSUPPORTED_RESULT_TYPE")
