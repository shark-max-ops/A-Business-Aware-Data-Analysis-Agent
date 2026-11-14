"""核对表图声明与冻结行；不解析标题，不执行隐式过滤或聚合。"""

from datetime import date

from metric_pilot.data.common import DataError
from metric_pilot.data.ranges import DateRange


def validate_display_scope(block, record, main_range):
    """
    作用：核对声明的分组可见性、分类人群及日期子范围
    输入：表图块、完整冻结结果及服务端主范围
    输出：无；不符时返回含修正建议的错误
    """
    scope = block.get("display_scope")
    if scope is None:
        return

    # 声明的维度必须实际可见；图表多维分类先由查询生成唯一组合列。
    columns = [column["name"] for column in record["artifact"]["columns"]]
    visible = block["columns"] if block["type"] == "table" else [block["x"]]
    dimensions = scope["dimensions"]
    if len(set(dimensions)) != len(dimensions) or not set(dimensions) <= set(visible):
        raise DataError(
            "REPORT_SCOPE_DIMENSIONS",
            {"instruction": "分组列须在表中展示；多维图表先查询唯一组合分类列，或改用完整表格。"},
        )

    # 人群声明只是断言；所有冻结行都必须属于声明集合，绝不静默丢行。
    for column, allowed in scope["population"].items():
        if column not in columns or not allowed or len(allowed) > 100:
            raise DataError(
                "REPORT_SCOPE_POPULATION",
                {
                    "column": column,
                    "instruction": "指定真实分类列和非空字符串集合；先查询所需人群的成功产物。",
                },
            )
        index = columns.index(column)
        if any(
            not isinstance(row[index], str) or row[index] not in allowed for row in record["rows"]
        ):
            raise DataError(
                "REPORT_SCOPE_POPULATION",
                {
                    "column": column,
                    "instruction": "产物含声明以外人群；先查询筛选产物，标题不能筛行。",
                },
            )

    # 日期子范围须在服务端固定范围内，并由结果中的日粒度列逐行支持。
    selected, column = scope["date_range"], scope["date_column"]
    if (selected is None) != (column is None):
        raise DataError("REPORT_SCOPE_DATE_COLUMN")
    if selected is None:
        return
    if main_range is None:
        raise DataError("REPORT_SCOPE_MAIN_RANGE_REQUIRED")
    main = DateRange.model_validate(main_range)
    selected = DateRange.model_validate(selected)
    if selected.start < main.start or selected.end_exclusive > main.end_exclusive:
        raise DataError(
            "REPORT_SCOPE_DATE_RANGE",
            {"instruction": "子范围须处于固定主范围内；改变主范围须新建调查或分支。"},
        )
    if column not in columns:
        raise DataError("REPORT_SCOPE_DATE_COLUMN")
    for row in record["rows"]:
        value = row[columns.index(column)]
        if isinstance(value, dict) and value.get("type") == "date":
            value = value.get("value")
        try:
            valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            valid = False
        if not valid or not selected.start <= value < selected.end_exclusive:
            raise DataError(
                "REPORT_SCOPE_DATE_ROWS",
                {"instruction": "须有日粒度日期列，所有行在子范围内；先查询筛选产物。"},
            )
