"""从已冻结的双对象计数生成比较首段。"""

from fractions import Fraction

import sqlglot
from sqlglot import exp

from metric_pilot.data.common import DataError, digest
from metric_pilot.reports.contracts import RenderedBlock
from metric_pilot.reports.values import NUMERIC, cell, numeric


def zero_reference(record, key, target, date_range):
    """仅接受服务端日期范围内的单表、单身份等值 COUNT(*)。"""
    meta = record["artifact"]
    tree = sqlglot.parse_one(meta["sql"], read="duckdb")
    if meta["truncated"] or len(record["rows"]) != 1 or not isinstance(tree, exp.Select):
        return None
    allowed = {"expressions", "from_", "from", "where"}
    if any(v and k not in allowed for k, v in tree.args.items()) or len(tree.expressions) != 1:
        return None
    from_clause = tree.args.get("from_") or tree.args.get("from")
    source = from_clause.this if from_clause else None
    if not isinstance(source, exp.Table) or source.sql().lower() != "v_flights":
        return None
    counted = tree.expressions[0].unalias()
    condition = tree.args.get("where")
    if counted.sql().upper() != "COUNT(*)" or not condition:
        return None
    predicates = (
        condition.this.flatten() if isinstance(condition.this, exp.And) else [condition.this]
    )
    identity = []
    for item in predicates:
        if (
            isinstance(item, (exp.GTE, exp.LT))
            and isinstance(item.this, exp.Column)
            and item.this.name == "flight_date"
        ):
            bound = "start" if isinstance(item, exp.GTE) else "end_exclusive"
            if isinstance(item.expression, exp.Literal) and item.expression.this == date_range.get(
                bound
            ):
                continue
        identity.append(item)
    if len(identity) != 1:
        return None
    predicate = identity[0]
    if not isinstance(predicate, exp.EQ) or not isinstance(predicate.this, exp.Column):
        return None
    literal = predicate.expression
    if predicate.this.name != key or not isinstance(literal, exp.Literal):
        return None
    if literal.is_string != isinstance(target, str) or literal.this != str(target):
        return None
    if numeric(record["rows"][0][0]) != 0:
        return None
    return {"artifact_id": meta["id"], "row": 0, "column": meta["columns"][0]["name"]}


def comparison_answer(check, artifacts, date_range):
    """只比较同一组内的整数计数，以交叉乘法确定高低。"""
    ident = check["artifact_id"]
    record = artifacts[ident]
    names = [col["name"] for col in record["artifact"]["columns"]]
    key, num, den = (check[name] for name in ("key", "numerator", "denominator"))
    if (
        len(check["expected"]) != 2
        or len({key, num, den}) != 3
        or not {key, num, den} <= set(names)
    ):
        raise DataError("COMPARISON_COUNT_COLUMNS")
    if any(
        col.endswith("_pct")
        or not NUMERIC.match(record["artifact"]["columns"][names.index(col)]["type"])
        for col in (num, den)
    ):
        raise DataError("REPORT_COUNT_UNIT_MISMATCH")
    segments, rates, zero_confirmed = [], [], []

    def add(text, ref=None):
        value = cell(artifacts, ref, None, False) if ref else None
        segments.append(
            {
                "text": value["display"] if value else text,
                "value": value,
                "key": f"v{len(segments)}" if value else None,
            }
        )

    for target in check["expected"]:
        matches = [i for i, row in enumerate(record["rows"]) if row[names.index(key)] == target]
        add(f"{target}：")
        if check["sql_contains_limit"] or check["truncated"] or len(matches) > 1:
            add("比较范围未确认。")
            continue
        if not matches:
            proof_id = check.get("zero_count_artifact_id")
            ref = zero_reference(artifacts[proof_id], key, target, date_range) if proof_id else None
            if ref:
                add("同范围记录数为")
                add("", ref)
                add("，比例无法计算。")
                zero_confirmed.append(target)
            else:
                add("当前结果未覆盖，记录数尚未确认。")
            continue
        idx = matches[0]

        def ref(col):
            return {"artifact_id": ident, "row": idx, "column": col}

        vals = [record["rows"][idx][names.index(col)] for col in (num, den)]
        if any(v is None for v in vals):
            add("计数缺失。")
            continue
        n, d = map(numeric, vals)
        if not (0 <= n <= d and n == int(n) and d == int(d)):
            raise DataError("COMPARISON_INVALID_COUNTS")
        add("", ref(num))
        add("/")
        add("", ref(den))
        if d:
            rates.append(Fraction(int(n), int(d)))
            add(f"（{100 * n / d:.2f}%）。")
        else:
            add("，零分母，比例无法计算。")
    if len(rates) < 2:
        add("无法比较两侧高低。")
    elif rates[0] == rates[1]:
        add("双方比例相同。")
    else:
        add(f"{check['expected'][int(rates[1] > rates[0])]} 的比例更高。")
    proof = {
        "comparison": check,
        "confirmed_zero": zero_confirmed,
        "artifact_id": ident,
        "result_hash": record["artifact"].get("result_hash"),
    }
    proof["proof_hash"] = digest(proof)
    return RenderedBlock(
        type="text",
        id="comparison_answer",
        title="本次比较",
        segments=segments,
        verification="checked_relation",
        proof=proof,
        evidence_ids=[ident] + ([check["zero_count_artifact_id"]] if zero_confirmed else []),
    ).model_dump()
