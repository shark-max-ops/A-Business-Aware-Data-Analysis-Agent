"""将行选择转为既有发现契约；不生成业务结论或改写冻结值。"""

from metric_pilot.agent.contracts import RecordFinding
from metric_pilot.agent.investigations import read_evidence_artifact
from metric_pilot.data.common import DataError, new_id
from metric_pilot.reports.values import NUMERIC


def canonical_finding(selection, query, run):
    """
    作用：从授权真实行生成占位符和同行绑定，保持原保存与报告校验路径
    输入：已验证行选择、查询服务及固定运行
    输出：规范发现参数，尚不写入数据库
    """
    values, bindings, lines = {}, [], []
    for index, observation in enumerate(selection.observations):
        page = read_evidence_artifact(query, run, observation.artifact_id, observation.row, 1)
        metadata = page["artifact"]
        columns = {column["name"]: column["type"] for column in metadata["columns"]}
        if len(columns) != len(metadata["columns"]):
            raise DataError("REPORT_AMBIGUOUS_COLUMNS")
        missing = [name for name in observation.columns if name not in columns]
        if missing:
            raise DataError(
                "REPORT_COLUMN_NOT_FOUND",
                {"missing_columns": missing, "available_columns": list(columns)},
            )
        if not page["rows"]:
            raise DataError("REPORT_ROW_NOT_FOUND", {"returned_rows": metadata["returned_rows"]})
        row = dict(zip(columns, page["rows"][0], strict=True))
        dimensions, measures, parts = [], [], []
        for position, name in enumerate(observation.columns):
            key = f"r{index}_c{position}"
            values[key] = {
                "artifact_id": observation.artifact_id,
                "row": observation.row,
                "column": name,
            }
            # 列标签只作普通文字，原始列名仍完整保存在引用中。
            label = name[:40].replace("{", "（").replace("}", "）")
            parts.append(label + "：{{" + key + "}}")
            if NUMERIC.match(columns[name]):
                if row[name] is not None:
                    measures.append(key)
            else:
                dimensions.append(key)
        # 分别绑定每条真实行；跨月比较不要求两个月份处于同一行。
        if dimensions and measures:
            bindings.append({"dimensions": dimensions, "measures": measures})
        lines.append("；".join(parts))
    if selection.note.strip():
        lines.append("待复核说明：" + selection.note.strip())
    return RecordFinding.model_validate(
        {
            "kind": selection.kind,
            "check": selection.check,
            "statement": {
                "type": "text",
                "id": new_id("observation"),
                "text_template": "\n".join(lines),
                "values": values,
                "row_bindings": bindings,
                "evidence_ids": selection.evidence_ids,
            },
            "questions": selection.questions,
            "supersedes": selection.supersedes,
        }
    )
