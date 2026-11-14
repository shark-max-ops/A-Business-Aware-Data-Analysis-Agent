"""在完整冻结结果集内生成可复算的排序与关系，不认证SQL业务含义。"""

from metric_pilot.data.common import DataError, canonical_bytes, digest
from metric_pilot.reports.values import NUMERIC, cell, numeric

POLICY = "checked_relations_v1"
LABELS = {
    "month": "月份",
    "month_start": "月份",
    "day": "日期",
    "flight_date": "航班日期",
    "carrier_code": "航司代码",
    "planned_flights": "计划航班数",
    "scheduled_flights": "计划航班数",
    "record_count": "记录数",
    "cancelled_flights": "取消数",
    "cancelled_count": "取消数",
    "cancellation_rate_pct": "取消比例",
    "cancellation_code": "上报取消原因代码",
}


def label(column):
    """作用：按实际字段提供显示名称；输入：真实列名；输出：普通文本标签。"""
    return LABELS.get(column, f"字段“{column}”")


def checked_record(block, artifacts):
    """
    作用：拒绝截断、缺列、非数值和不完整身份，固定可比较集合
    输入：排名或关系块及已授权冻结产物
    输出：完整产物和实际列位置
    """
    record = artifacts[block["artifact_id"]]
    metadata = record["artifact"]
    if metadata["truncated"] or len(record["rows"]) != metadata["returned_rows"]:
        raise DataError("REPORT_CLAIM_INCOMPLETE")
    names = [c["name"] for c in metadata["columns"]]
    requested = [*block["dimensions"], block["measure"]]
    if len(set(requested)) != len(requested) or not set(requested) <= set(names):
        raise DataError("REPORT_CLAIM_COLUMNS", {"available_columns": names})
    if not NUMERIC.match(metadata["columns"][names.index(block["measure"])]["type"]):
        raise DataError("REPORT_VALUE_TYPE")
    identities = [
        canonical_bytes([row[names.index(c)] for c in block["dimensions"]])
        for row in record["rows"]
    ]
    if len(set(identities)) != len(identities):
        raise DataError(
            "REPORT_CLAIM_IDENTITY",
            {"instruction": "身份列必须能唯一识别结果集内每行；跨月航司须包含月份与航司。"},
        )
    return record, names


def render_relation(block, artifacts):
    """
    作用：计算完整结果集的名次或两行关系，生成不可由模型改写的文字与证明
    输入：已验证结构和冻结数据
    输出：含真实单元格引用、计算范围、空值与并列规则的显示块
    """
    record, names = checked_record(block, artifacts)
    measure_index = names.index(block["measure"])
    rows = record["rows"]
    segments = []

    def text(value):
        """作用：追加服务端固定文字；输入：文字；输出：无。"""
        segments.append({"text": value})

    def referenced(row, column):
        """作用：追加可定位的原始单元格；输入：行列；输出：无。"""
        value = cell(artifacts, {"artifact_id": block["artifact_id"], "row": row, "column": column})
        segments.append(
            {"text": value["display"], "key": f"r{row}_{names.index(column)}", "value": value}
        )

    def identity(row):
        """作用：显示实际完整身份；输入：绝对行号；输出：无。"""
        for index, column in enumerate(block["dimensions"]):
            if index:
                text("、")
            text(label(column) + "=")
            referenced(row, column)

    proof = {
        "version": POLICY,
        "operation": block["type"],
        "artifact_id": block["artifact_id"],
        "result_hash": record["artifact"]["result_hash"],
        "comparison_rows": len(rows),
        "dimensions": block["dimensions"],
        "measure": block["measure"],
        "scope": "仅此完整冻结结果集；不证明原SQL总体覆盖、分母、因果或显著性。",
    }
    if block["type"] == "ranking":
        valid = [
            (index, numeric(row[measure_index]))
            for index, row in enumerate(rows)
            if row[measure_index] is not None
        ]
        valid.sort(key=lambda pair: pair[1], reverse=block["direction"] == "descending")
        ranks, previous, rank = [], None, 0
        for position, (index, value) in enumerate(valid, 1):
            if value != previous:
                rank = position
            previous = value
            if rank <= block["top_k"]:
                ranks.append({"row": index, "rank": rank})
        proof.update(
            direction=block["direction"],
            top_k=block["top_k"],
            null_rows=len(rows) - len(valid),
            tie_policy="competition_rank_include_all_ties",
            ranked_rows=ranks,
        )
        text(
            f"在此查询结果的{len(rows)}条记录中，按{label(block['measure'])}"
            + ("从高到低" if block["direction"] == "descending" else "从低到高")
            + f"取前{block['top_k']}名（并列全部保留；空值不排名）：\n"
        )
        if not ranks:
            text("没有可排名的非空数值。")
        for item in ranks:
            text(f"第{item['rank']}名：")
            identity(item["row"])
            text("；" + label(block["measure"]) + "=")
            referenced(item["row"], block["measure"])
            text("。\n")
    else:
        left, right = block["left_row"], block["right_row"]
        if left == right or max(left, right) >= len(rows):
            raise DataError("REPORT_CLAIM_ROWS")
        a, b = rows[left][measure_index], rows[right][measure_index]
        relation = (
            "unavailable"
            if a is None or b is None
            else (
                "higher"
                if numeric(a) > numeric(b)
                else "lower"
                if numeric(a) < numeric(b)
                else "equal"
            )
        )
        proof.update(left_row=left, right_row=right, relation=relation)
        identity(left)
        text("的" + label(block["measure"]) + "=")
        referenced(left, block["measure"])
        text("；")
        identity(right)
        text("的" + label(block["measure"]) + "=")
        referenced(right, block["measure"])
        text(
            "。存在缺失，无法判断两者高低。"
            if relation == "unavailable"
            else "。前者"
            + {"higher": "高于", "lower": "低于", "equal": "等于"}[relation]
            + "后者；仅描述数值关系。"
        )
    proof["proof_hash"] = digest(proof)
    return {
        "type": "text",
        "id": block["id"],
        "title": "已核对的结果集关系",
        "segments": segments,
        "verification": "checked_relation",
        "proof": proof,
        "artifact_id": block["artifact_id"],
    }
