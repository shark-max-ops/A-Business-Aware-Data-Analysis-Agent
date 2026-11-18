"""按用户声明的比较对象核对冻结行；沿用现有调查授权与分页。"""

from decimal import Decimal

import sqlglot
from sqlglot import exp

from metric_pilot.agent.investigations import read_evidence_artifact
from metric_pilot.data.common import DataError


def comparison_coverage(query, run, check):
    """扫描成功产物全部冻结行，区分缺组、分母缺失、查询限行与存储截断。"""
    offset = 0
    seen = set()
    unknown = set()
    while True:
        page = read_evidence_artifact(query, run, check.artifact_id, offset, 100)
        metadata = page["artifact"]
        columns = [column["name"] for column in metadata["columns"]]
        if check.key not in columns or (check.denominator and check.denominator not in columns):
            raise DataError("COMPARISON_COLUMN_NOT_FOUND")
        key = columns.index(check.key)
        denominator = columns.index(check.denominator) if check.denominator else None
        for row in page["rows"]:
            group = row[key]
            if isinstance(group, dict):
                if group["type"] == "integer":
                    group = int(group["value"])
                else:
                    group = group["value"]
            if type(group) in (int, str) and group in check.expected:
                seen.add(group)
                if denominator is not None:
                    value = row[denominator]
                    if isinstance(value, dict) and value["type"] in ("decimal", "integer"):
                        value = Decimal(value["value"])
                    if type(value) not in (int, float, Decimal) or value <= 0:
                        unknown.add(group)
        offset = page["next_offset"]
        if offset is None:
            break
    missing = [v for v in check.expected if v not in seen]
    unknown = [v for v in check.expected if v in unknown]
    limited = bool(sqlglot.parse_one(metadata["sql"], read="duckdb").find(exp.Limit))
    gaps = []
    if missing:
        gaps.append(f"{check.key} 在冻结结果中未观察到 " + "、".join(map(str, missing)))
    if unknown:
        gaps.append(f"{check.denominator} 缺少有效正分母：" + "、".join(map(str, unknown)))
    if metadata["truncated"]:
        gaps.append("冻结结果已截断，比较总体仍待核查")
    return {
        **check.model_dump(),
        "missing": missing,
        "unknown_denominator": unknown,
        "truncated": metadata["truncated"],
        "sql_contains_limit": limited,
        "rows_checked": metadata["returned_rows"],
        "gaps": gaps,
    }
