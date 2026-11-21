"""双侧比较首段仅从冻结行和同范围零计数生成。"""

import pytest
from metric_pilot.data.common import DataError
from metric_pilot.reports.comparison_answer import comparison_answer


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
