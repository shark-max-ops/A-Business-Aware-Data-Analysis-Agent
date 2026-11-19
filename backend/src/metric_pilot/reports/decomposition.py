"""从冻结分组计数核对两期各组对总体率差的算术贡献。"""

from decimal import Decimal, localcontext
from fractions import Fraction

from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.reports.values import numeric


def exact(value):
    """保留精确分子分母并生成仅用于显示的四位小数。"""
    with localcontext() as context:
        context.prec = len(str(value.denominator)) + 12
        display = format(Decimal(value.numerator) / Decimal(value.denominator), ".4f")
    return dict(
        exact_numerator=str(value.numerator),
        exact_denominator=str(value.denominator),
        display=display,
    )


def decompose(block, rows, names):
    """比较两期同一分类，独立合计行固定分母与待还原的总差。"""
    period, group = (names.index(c) for c in block["dimensions"])
    part, total = (names.index(block[c]) for c in ("measure", "denominator"))
    anchors = [block["comparison_total_row"], block["baseline_total_row"]]
    if max(anchors) >= len(rows):
        raise DataError("REPORT_CLAIM_ROWS")
    periods = [canonical_bytes(rows[i][period]) for i in anchors]
    if len(set(periods)) != 2 or any(rows[i][period] is None for i in anchors):
        raise DataError("REPORT_PERIOD_MISMATCH")
    if canonical_bytes(rows[anchors[0]][group]) != canonical_bytes(rows[anchors[1]][group]):
        raise DataError("REPORT_TOTAL_GROUP_MISMATCH")

    def counts(i):
        """读取同一行中非负、整数且子集不超过总体的计数。"""
        if any(rows[i][c] is None for c in (part, total)):
            raise DataError("REPORT_COUNT_MISSING", {"row": i})
        a, b = (numeric(rows[i][c]) for c in (part, total))
        if not 0 <= a <= b or any(v != int(v) for v in (a, b)):
            raise DataError("REPORT_INVALID_COUNTS", {"row": i})
        return int(a), int(b)

    totals = [counts(i) for i in anchors]
    if any(b == 0 for _, b in totals):
        raise DataError("REPORT_ZERO_DENOMINATOR")
    sides = [{}, {}]
    for i, row in enumerate(rows):
        if i in anchors:
            continue
        key = canonical_bytes(row[period])
        if key not in periods or row[group] is None:
            raise DataError("REPORT_GROUP_SCOPE", {"row": i})
        sides[periods.index(key)][canonical_bytes(row[group])] = (i, *counts(i))
    if not sides[0] or sides[0].keys() != sides[1].keys():
        raise DataError("REPORT_GROUP_COVERAGE")
    for side, expected in zip(sides, totals, strict=True):
        if tuple(sum(item[c] for item in side.values()) for c in (1, 2)) != expected:
            raise DataError("REPORT_GROUP_TOTAL_MISMATCH")
    entries, contributions = [], []
    for key, (i, a, _) in sides[0].items():
        j, b, _ = sides[1][key]
        delta = Fraction(100 * a, totals[0][1]) - Fraction(100 * b, totals[1][1])
        entries.append(dict(comparison_row=i, baseline_row=j, **exact(delta)))
        contributions.append(delta)
    result = Fraction(100 * totals[0][0], totals[0][1]) - Fraction(100 * totals[1][0], totals[1][1])
    if sum(contributions) != result:
        raise DataError("REPORT_GROUP_TOTAL_MISMATCH")
    return dict(
        groups=entries,
        total_rates=[exact(Fraction(100 * a, b)) for a, b in totals],
        comparison_total_row=anchors[0],
        baseline_total_row=anchors[1],
        denominator=block["denominator"],
        unit="个百分点",
        **exact(result),
    )
