"""阶段7独立验收器不能以错误分母、身份或缺组结果通过。"""

import json
from pathlib import Path

import pytest
import test_multimonth_verification

sample = test_multimonth_verification.sample


@pytest.fixture
def verifier(monkeypatch):
    """
    作用：加载复用原CSV基线的阶段7验收器
    输入：测试模块路径隔离
    输出：验收模块
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import verify_stage7

    return verify_stage7


@pytest.mark.parametrize(
    "change,error",
    [
        ("none", None),
        ("aliases", None),
        ("denominator", "INDEPENDENT_VALUE_MISMATCH"),
        ("identity", "GROUP_IDENTITY_MISMATCH"),
        ("missing", "INDEPENDENT_CHECK_INCOMPLETE"),
        ("unknown", "UNVERIFIED_COLUMN"),
        ("unreviewed", "FORMAL_METRIC_BINDING_REQUIRED"),
        ("unreviewed_alias", "FORMAL_METRIC_BINDING_REQUIRED"),
    ],
)
def test_independent_checker_rejects_invalid_groups(verifier, sample, tmp_path, change, error):
    """
    作用：验证完整真实形状的合成结果及错误身份、分母和正式口径拒绝
    输入：合成基线、案例与反例类型
    输出：无
    """
    reference, case = sample
    item = case["artifacts"]["artifact"]
    item["artifact"]["sql"] = "SELECT 1"
    if change == "denominator":
        item["rows"][0][1] = 1
    elif change == "identity":
        item["rows"][1][0] = "2026-01"
    elif change == "missing":
        item["rows"].pop()
    elif change == "unknown":
        item["artifact"]["columns"][1]["name"] = "invented"
    elif change == "aliases":
        item["artifact"]["columns"][1]["name"] = "total_records"
        item["artifact"]["columns"][-1]["name"] = "overall_rate"
    elif change in {"unreviewed", "unreviewed_alias"}:
        item["artifact"]["metric_revision_ids"] = []
        if change == "unreviewed_alias":
            item["artifact"]["columns"][-1]["name"] = "overall_rate"
    (tmp_path / "baseline.json").write_text(json.dumps(reference), encoding="utf-8")
    (tmp_path / "monthly.json").write_text(json.dumps(case), encoding="utf-8")
    if error:
        with pytest.raises(RuntimeError, match=error):
            verifier.check_case(tmp_path, "monthly")
    else:
        result = verifier.check_case(tmp_path, "monthly")
        assert result["numeric_passed"] and result["values"] == 6
        check = json.loads((tmp_path / "monthly-check.json").read_text(encoding="utf-8"))
        assert check["semantic_accepted"] is False
