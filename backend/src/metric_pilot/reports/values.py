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
        raise DataError(
            "REPORT_COLUMN_NOT_FOUND",
            {
                "column": ref.column,
                "available_columns": names[:64],
                "artifact_id": ref.artifact_id,
            },
        )
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
    # 排名前缀限定了子集，子集内的数量须来自聚合结果。
    if re.search(
        r"前\s*(?:\d+|[一二三四五六七八九十百]+)\s*(?:条|行|班|名)(?:记录)?"
        r"\s*(?:中|里|之中)\s*(?:仅|共有|共|有)?\s*(?:\d+|[一二三四五六七八九十百]+)\s*(?:条|行|班|名)",
        text,
    ):
        raise DataError(
            "REPORT_SUBSET_COUNT_UNBOUND",
            {
                "instruction": "对相同排序、并列和行数范围单独聚合，引用计数列；"
                "无关的额外计数请省略。"
            },
        )
    slots = list(SLOT.finditer(text))
    remaining = SLOT.sub("", text)
    if any(token in remaining for token in ("{{", "}}", "{%", "{#")):
        raise DataError("REPORT_TEMPLATE_INVALID")
    if {match[1] for match in slots} != set(block["values"]):
        raise DataError(
            "REPORT_TEMPLATE_KEYS",
            {
                "missing_refs": sorted({match[1] for match in slots} - set(block["values"])),
                "unused_refs": sorted(set(block["values"]) - {match[1] for match in slots}),
            },
        )
    # 绑定只使用已展示的引用键，核对分组和值属于同一冻结结果行。
    for binding in block.get("row_bindings", []):
        keys = binding["dimensions"] + binding["measures"]
        if len(keys) != len(set(keys)) or not set(keys) <= set(block["values"]):
            raise DataError(
                "REPORT_ROW_BINDING_KEYS",
                {
                    "available_keys": list(block["values"]),
                    "binding_keys": keys,
                    "instruction": "dimensions/measures填写values中的占位键，不是原始列名；"
                    "身份也须在模板和values中引用；每组分别绑定，必要时减少叙述组数。",
                },
            )
        refs = [block["values"][key] for key in keys]
        if len({(ref["artifact_id"], ref["row"]) for ref in refs}) != 1:
            raise DataError(
                "REPORT_ROW_BINDING_MISMATCH",
                {
                    "references": {key: block["values"][key] for key in keys},
                    "instruction": "每组身份与数量须同产物同行；不同组即使月份相同，"
                    "也须各自引用对应行的月份键，不共享另一行的键。",
                },
            )
        for key in binding["measures"]:
            cell(artifacts, block["values"][key], require_numeric=True)
    result, position = [], 0
    for match in slots:
        if match.start() > position:
            result.append({"text": text[position : match.start()]})
        count_slot = re.search(r"(?:共计|合计|共有|共)\s*$", text[: match.start()]) and re.match(
            r"\s*[个家班条次天名座项种]", text[match.end() :]
        )
        value = cell(artifacts, block["values"][match[1]], require_numeric=bool(count_slot))
        column = value["value_ref"]["column"]
        suffix = text[match.end() :]
        # 两侧已从同一列的不同记录取值时，其中任何一侧都不能充当差额。
        if re.search(
            r"(?:比[^。，；]{1,18}[多少]|减少|增加|相差)\s*(?:约)?\s*$", text[: match.start()]
        ) and re.match(r"\s*班", suffix):
            ref = value["value_ref"]
            prior = [s["value"]["value_ref"] for s in result if "value" in s]
            if ref in prior and any(
                r["artifact_id"] == ref["artifact_id"]
                and r["column"] == column
                and r["row"] != ref["row"]
                for r in prior
            ):
                raise DataError(
                    "REPORT_DIFFERENCE_REF",
                    {
                        "column": column,
                        "instruction": "此引用是比较一侧的原值。使用冻结差额列，"
                        "或保留两侧数值与独立建议。",
                    },
                )
        if re.match(r"\s*班", suffix) and (
            column == "days" or column.startswith("days_with_") or column.endswith("_days")
        ):
            raise DataError(
                "REPORT_VALUE_TYPE",
                {
                    "column": column,
                    "instruction": "天数列不能填写航班数，引用航班计数列或改写天数单位。",
                },
            )
        # 明确的每百班表达与百分比数值同尺度，量词由模板提供。
        per_hundred = (
            column.endswith("_pct")
            and re.search(
                r"每(?:百|100)班(?:航班)?(?:中)?(?:约|大约)?(?:有|取消|备降|延误)?(?:约|大约)?\s*$",
                text[: match.start()],
            )
            and re.match(r"\s*班", suffix)
        )
        named_count = (
            column.endswith("_pct")
            and re.search(
                r"每(?:百|100)班(?:航班)?(?:取消|备降|延误)(?:班次|数)(?:约为|为)?\s*$",
                text[: match.start()],
            )
            and re.match(r"\s*(?:[，。；、（(]|$)", suffix)
        )
        if named_count:
            value["display"] = value["display"].removesuffix("%") + " 班"
        elif per_hundred:
            value["display"] = value["display"].removesuffix("%")
        elif column.endswith("_pct") and re.match(
            r"^\s*(?:[%％]|个百分点|班(?:次)?(?:[（(，。、；\s]|$))", text[match.end() :]
        ):
            raise DataError(
                "REPORT_DUPLICATE_PERCENT",
                {
                    "column": column,
                    "instruction": "该列已显示百分号。若表达每百班，可写为取消率加该引用；"
                    "需按班表达时查询独立无百分号的每百班数列。"
                    "百分点差必须引用差值列。",
                },
            )
        result.append({"text": value["display"], "key": match[1], "value": value})
        position = match.end()
    if position < len(text):
        result.append({"text": text[position:]})
    return result
