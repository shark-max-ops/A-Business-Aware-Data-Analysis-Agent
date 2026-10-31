"""Decimal 安全格式化与无执行能力的文字占位符。"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from metric_pilot.data.common import DataError
from metric_pilot.reports.contracts import FrozenValue, ValueRef

NUMERIC = re.compile(
    r"^(U?TINYINT|U?SMALLINT|U?INTEGER|U?BIGINT|U?HUGEINT|FLOAT|DOUBLE|REAL|DECIMAL\b)"
)
SLOT = re.compile(r"{{\s*([A-Za-z_][A-Za-z_0-9]*)\s*}}")


def numeric(raw):
    """
    作用：从真实数值或保精度标签读取 Decimal，拒绝布尔与字符串冒充数值
    输入：冻结单元格
    输出：有限 Decimal
    """
    if isinstance(raw, dict) and raw.get("type") in {"decimal", "integer"}:
        raw = raw["value"]
    elif type(raw) not in {int, float}:
        raise DataError("REPORT_VALUE_TYPE")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise DataError("REPORT_VALUE_TYPE") from None
    if not value.is_finite():
        raise DataError("REPORT_NONFINITE_VALUE")
    return value


def display_value(raw, fmt=None, column=""):
    """
    作用：在服务端格式化原值，不推导指标或二次放大百分比
    输入：真实值、可选格式和原始列名
    输出：可直接展示的中文文本
    """
    if raw is None:
        return "无可用值"
    if fmt is None:
        if isinstance(raw, dict):
            if raw.get("type") not in {"decimal", "integer", "date", "datetime", "time"}:
                raise DataError("REPORT_VALUE_TYPE")
            return str(raw["value"])
        if isinstance(raw, bool):
            return "是" if raw else "否"
        if isinstance(raw, str):
            return raw
        value = numeric(raw)
        return format(value, ",f") if value == value.to_integral_value() else str(value)
    value = numeric(raw)
    if fmt == "percent_2" and (not column.endswith("_pct") or not 0 <= value <= 100):
        raise DataError("REPORT_PERCENT_SCALE")
    if fmt == "integer":
        if value != value.to_integral_value():
            raise DataError("REPORT_INTEGER_FORMAT")
        return format(value, ",.0f")
    with localcontext() as context:
        context.prec = max(
            80, len(value.as_tuple().digits) + max(value.as_tuple().exponent, 0) + 10
        )
        formatted = format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",.2f")
    return formatted + ("%" if fmt == "percent_2" else " 分钟" if fmt == "minutes_2" else "")


def cell(artifacts, ref, fmt=None, require_numeric=False):
    """
    作用：只从已验证冻结产物中按行列取值
    输入：冻结产物字典、引用和显示约束
    输出：服务端物化单元格
    """
    ref = ValueRef.model_validate(ref)
    artifact = artifacts.get(ref.artifact_id)
    if artifact is None:
        raise DataError("REPORT_ARTIFACT_NOT_ALLOWED")
    names = [column["name"] for column in artifact["artifact"]["columns"]]
    if ref.column not in names:
        raise DataError("REPORT_COLUMN_NOT_FOUND", {"column": ref.column})
    if ref.row >= len(artifact["rows"]):
        raise DataError("REPORT_ROW_OUT_OF_RANGE")
    index = names.index(ref.column)
    if fmt is None and ref.column.endswith("_pct"):
        fmt, require_numeric = "percent_2", True
    if require_numeric and not NUMERIC.match(artifact["artifact"]["columns"][index]["type"]):
        raise DataError("REPORT_VALUE_TYPE", {"column": ref.column})
    raw = artifact["rows"][ref.row][index]
    if require_numeric and raw is not None:
        numeric(raw)
    return FrozenValue(
        raw=raw, display=display_value(raw, fmt, ref.column), value_ref=ref
    ).model_dump()


def text_segments(block, artifacts):
    """
    作用：仅替换命名占位符，保留普通文字与可点击值的区别
    输入：说明块和冻结产物
    输出：安全展示片段
    """
    text = block["text_template"]
    slots = list(SLOT.finditer(text))
    remaining = SLOT.sub("", text)
    if any(token in remaining for token in ("{{", "}}", "{%", "{#")):
        raise DataError("REPORT_TEMPLATE_INVALID")
    if {match[1] for match in slots} != set(block["values"]):
        raise DataError("REPORT_TEMPLATE_KEYS")
    result, position = [], 0
    for match in slots:
        if match.start() > position:
            result.append({"text": text[position : match.start()]})
        value = cell(artifacts, block["values"][match[1]])
        if value["value_ref"]["column"].endswith("_pct") and re.match(
            r"^\s*[%％]", text[match.end() :]
        ):
            raise DataError("REPORT_DUPLICATE_PERCENT")
        result.append({"text": value["display"], "key": match[1], "value": value})
        position = match.end()
    if position < len(text):
        result.append({"text": text[position:]})
    return result
