"""双侧比较首段仅从冻结行和同范围零计数生成。"""

import pytest
from metric_pilot.data.common import DataError
from metric_pilot.reports.comparison_answer import comparison_answer, zero_reference


def answer(rows, proof_sql=None, limited=False):
    columns = [
        {"name": k, "type": t}
        for k, t in (("carrier_code", "VARCHAR"), ("n", "BIGINT"), ("d", "BIGINT"))
    ]
    artifacts = {"groups": {"rows": rows, "artifact": {"id": "groups", "columns": columns}}}
    check = dict(
        artifact_id="groups",
        key="carrier_code",
        numerator="n",
        denominator="d",
        expected=["ZX", "ZZ"],
        truncated=False,
        sql_contains_limit=limited,
    )
    if proof_sql:
        artifacts["z"] = {
            "rows": [[0]],
            "artifact": {
                "id": "z",
                "sql": proof_sql,
                "columns": [{"name": "n", "type": "BIGINT"}],
                "truncated": False,
            },
        }
        check["zero_count_artifact_id"] = "z"
    return comparison_answer(
        check, artifacts, {"start": "2026-01-01", "end_exclusive": "2026-02-01"}
    )


@pytest.mark.parametrize(
    "suffix", [" AND cancelled=1", " AND flight_date >= '2026-01-02'", " LIMIT 1"]
)
def test_filtered_count_does_not_prove_absence(suffix):
    result = answer(
        [["ZX", 1, 5]], "SELECT COUNT(*) FROM v_flights WHERE carrier_code='ZZ'" + suffix
    )
    assert not result["proof"]["confirmed_zero"]


def test_proven_missing_group():
    result = answer(
        [["ZX", 1, 5]],
        "SELECT COUNT(*) FROM v_flights WHERE carrier_code='ZZ' "
        "AND flight_date >= '2026-01-01' AND flight_date < '2026-02-01'",
    )
    assert result["proof"]["confirmed_zero"] == ["ZZ"]
    assert any(
        s["value"] and s["value"]["value_ref"]["artifact_id"] == "z" for s in result["segments"]
    )


@pytest.mark.parametrize(
    ("rows", "limited", "expected"),
    [
        ([["ZX", 1, 5], ["ZZ", 0, 3]], False, "ZX 的比例更高"),
        ([["ZX", 1, 5], ["ZZ", 2, 10]], False, "双方比例相同"),
        ([["ZX", 1, 5]], False, "记录数尚未确认"),
        ([["ZX", 1, 5]], True, "比较范围未确认"),
    ],
)
def test_comparison_status(rows, limited, expected):
    result = answer(rows, limited=limited)
    assert expected in "".join(s["text"] for s in result["segments"])


def test_unrelated_zero_count_is_not_evidence():
    result = answer([["ZX", 1, 5]], "SELECT COUNT(*) FROM v_flights WHERE carrier_code='ZY'")
    assert not result["proof"]["confirmed_zero"]


def test_invalid_counts_rejected():
    with pytest.raises(DataError, match="COMPARISON_INVALID_COUNTS"):
        answer([["ZX", 6, 5], ["ZZ", 0, 3]])


def test_independent_zero_conflicts_with_positive_denominator():
    with pytest.raises(DataError, match="COMPARISON_CONFLICTING_COUNTS"):
        answer(
            [["ZX", 1, 5], ["ZZ", 0, 3]],
            "SELECT COUNT(*) FROM v_flights WHERE carrier_code='ZZ'",
        )


def test_zero_denominator_does_not_establish_equal_rates():
    result = answer([["ZX", 0, 5], ["ZZ", 0, 0]])
    summary = "".join(segment["text"] for segment in result["segments"])
    assert "零分母" in summary and "无法比较两侧高低" in summary


@pytest.mark.parametrize(("end", "confirmed"), [("2026-01-03", True), ("2026-02-01", False)])
def test_single_date_count_covers_only_matching_one_day_scope(end, confirmed):
    record = {
        "artifact": {
            "id": "zero",
            "sql": "SELECT COUNT(*) AS n FROM v_flights WHERE flight_date = DATE '2026-01-02' "
            "AND carrier_code = 'ZZ'",
            "truncated": False,
            "columns": [{"name": "n"}],
        },
        "rows": [[0]],
    }
    scope = {"start": "2026-01-02", "end_exclusive": end}
    assert bool(zero_reference(record, "carrier_code", "ZZ", scope)) is confirmed


def test_checked_kpi_uses_identity_from_the_referenced_row():
    from metric_pilot.reports.claims import POLICY
    from metric_pilot.reports.contracts import ReportDraft
    from metric_pilot.reports.materialize import render_blocks

    draft = ReportDraft(
        schema_version="1",
        title="synthetic",
        scope="synthetic",
        blocks=[
            dict(
                type="kpi",
                id="count",
                title="unverified group",
                value_ref=dict(artifact_id="qa_test", row=1, column="n"),
                format="integer",
            )
        ],
    )
    record = {
        "artifact": {
            "columns": [
                {"name": "carrier_code", "type": "VARCHAR"},
                {"name": "n", "type": "INTEGER"},
            ]
        },
        "rows": [["ZX", 1], ["ZZ", 0]],
    }
    blocks, _ = render_blocks(draft, {"qa_test": record}, {}, policy=POLICY)
    assert "ZZ" in blocks[0]["title"] and "ZX" not in blocks[0]["title"]
    assert blocks[0]["value"]["raw"] == 0


def test_shared_filter_zero_count_confirmed_and_unshared_rejected():
    columns = [
        {"name": k, "type": t}
        for k, t in (("carrier_code", "VARCHAR"), ("n", "BIGINT"), ("d", "BIGINT"))
    ]
    base_sql = (
        "SELECT carrier_code, SUM(cancelled) AS n, COUNT(*) AS d FROM v_flights "
        "WHERE origin_code = 'ORD' AND carrier_code IN ('ZZ', 'UA') "
        "AND flight_date >= '2026-01-01' AND flight_date < '2026-02-01' "
        "GROUP BY carrier_code"
    )
    scope = {"start": "2026-01-01", "end_exclusive": "2026-02-01"}

    def run_case(proof_sql):
        artifacts = {
            "groups": {
                "rows": [["UA", 139, 7205]],
                "artifact": {"id": "groups", "sql": base_sql, "columns": columns},
            },
            "z": {
                "rows": [[0]],
                "artifact": {
                    "id": "z",
                    "sql": proof_sql,
                    "columns": [{"name": "zz_cnt", "type": "BIGINT"}],
                    "truncated": False,
                },
            },
        }
        check = dict(
            artifact_id="groups",
            key="carrier_code",
            numerator="n",
            denominator="d",
            expected=["ZZ", "UA"],
            truncated=False,
            sql_contains_limit=False,
            zero_count_artifact_id="z",
        )
        return comparison_answer(check, artifacts, scope)

    confirmed = run_case(
        "SELECT COUNT(*) AS zz_cnt FROM v_flights WHERE origin_code = 'ORD' "
        "AND carrier_code = 'ZZ' AND flight_date >= '2026-01-01' AND flight_date < '2026-02-01'"
    )
    assert confirmed["proof"]["confirmed_zero"] == ["ZZ"]
    assert "同范围记录数为0，比例无法计算" in "".join(s["text"] for s in confirmed["segments"])

    rejected = run_case(
        "SELECT COUNT(*) AS zz_cnt FROM v_flights WHERE origin_code = 'ORD' "
        "AND dest_code = 'JFK' AND carrier_code = 'ZZ' "
        "AND flight_date >= '2026-01-01' AND flight_date < '2026-02-01'"
    )
    assert not rejected["proof"]["confirmed_zero"]
    assert "记录数尚未确认" in "".join(s["text"] for s in rejected["segments"])
