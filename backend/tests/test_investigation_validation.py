"""独立 CSV 基线检查不依赖报告布局，也不接受缺日、错值或重复日期。"""

import copy
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["valid", "wrong_total", "wrong_day", "duplicate_date"])
def test_independent_counts_across_findings_and_report(monkeypatch, case):
    """
    作用：核对总数仅在发现出现时仍可验收，且逐日统计必须真实匹配
    输入：合成冻结报告与发现及错误场景
    输出：无
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from verify_investigations import check_independent_counts

    # 报告只展示逐日结果，总数在另一项发现中；日期顺序不影响按键比较。
    counts = {"2026-01-01": 1, "2026-01-02": 2}
    frozen = {
        "blocks": [],
        "artifacts": {
            "qa_synthetic": {
                "artifact": {
                    "columns": [{"name": "day", "type": "DATE"}, {"name": "n", "type": "BIGINT"}]
                },
                "rows": [
                    [{"type": "date", "value": "2026-01-02"}, 2],
                    [{"type": "date", "value": "2026-01-01"}, 1],
                ],
            }
        },
    }
    finding = {"block": {"segments": [{"value": {"raw": 3}}]}, "artifacts": {}}
    original = copy.deepcopy(frozen)
    if case == "wrong_total":
        finding["block"]["segments"][0]["value"]["raw"] = 99
    elif case == "wrong_day":
        frozen["artifacts"]["qa_synthetic"]["rows"][0][1] = 99
    elif case == "duplicate_date":
        frozen["artifacts"]["qa_synthetic"]["rows"][0][0]["value"] = "2026-01-01"
    if case == "valid":
        assert check_independent_counts(frozen, [finding], {"rows": 3}, counts) == {
            "total_matches": True,
            "daily_dates_matched": 2,
        }
        assert frozen == original
    else:
        with pytest.raises(RuntimeError, match="INDEPENDENT_"):
            check_independent_counts(frozen, [finding], {"rows": 3}, counts)
