"""合成分组陈述的同行引用校验。"""

import copy

import pytest
from metric_pilot.data.common import DataError
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.materialize import render_blocks


def sample():
    """
    作用：构造两行合成分组及其报告文字
    输入：无
    输出：报告草稿和冻结结果
    """

    def ref(row, column):
        """
        作用：构造合成单元格引用
        输入：行号和列名
        输出：引用字典
        """
        return {"artifact_id": "qa_test", "row": row, "column": column}

    draft = {
        "schema_version": "1",
        "title": "测试",
        "scope": "合成",
        "blocks": [
            {
                "type": "text",
                "id": "t",
                "text_template": "{{carrier}} {{month}} {{count}}",
                "values": {
                    "carrier": ref(0, "carrier"),
                    "month": ref(0, "month"),
                    "count": ref(0, "count"),
                },
                "row_bindings": [{"dimensions": ["carrier", "month"], "measures": ["count"]}],
            }
        ],
    }
    artifacts = {
        "qa_test": {
            "artifact": {
                "columns": [
                    {"name": "carrier", "type": "VARCHAR"},
                    {"name": "month", "type": "VARCHAR"},
                    {"name": "count", "type": "BIGINT"},
                ]
            },
            "rows": [["OH", "1月", 120], ["OO", "1月", 200]],
        }
    }
    return draft, artifacts


def test_row_binding_materializes_real_cells():
    """
    作用：验证同行分组文字由冻结单元格生成并保留绑定
    输入：合成报告
    输出：无
    """
    draft, artifacts = sample()
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "OH 1月 120"
    assert blocks[0]["row_bindings"] == draft["blocks"][0]["row_bindings"]


@pytest.mark.parametrize(
    "change,error",
    [
        ("row", "REPORT_ROW_BINDING_MISMATCH"),
        ("artifact", "REPORT_ROW_BINDING_MISMATCH"),
        ("missing", "REPORT_ROW_BINDING_KEYS"),
        ("duplicate", "REPORT_ROW_BINDING_KEYS"),
        ("nonnumeric", "REPORT_VALUE_TYPE"),
    ],
)
def test_invalid_row_binding_rejected(change, error):
    """
    作用：拒绝错行、跨产物、缺键、重复键和非数值的绑定
    输入：错误类型与预期错误
    输出：无
    """
    draft, artifacts = sample()
    block = draft["blocks"][0]
    if change == "row":
        block["values"]["count"]["row"] = 1
    elif change == "artifact":
        artifacts["qa_other"] = copy.deepcopy(artifacts["qa_test"])
        block["values"]["count"]["artifact_id"] = "qa_other"
    elif change == "missing":
        block["row_bindings"][0]["dimensions"].append("unknown")
    elif change == "duplicate":
        block["row_bindings"][0]["dimensions"].append("count")
    else:
        block["values"]["count"]["column"] = "carrier"
    with pytest.raises(DataError, match=error):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})


def test_old_text_without_binding_still_renders():
    """
    作用：验证旧文字契约无需新增字段即可读取和物化
    输入：无绑定的合成报告
    输出：无
    """
    draft, artifacts = sample()
    del draft["blocks"][0]["row_bindings"]
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert blocks[0]["row_bindings"] == []


def test_mismatch_feedback_locates_each_group_reference():
    """
    作用：错行反馈明确指出各键的位置，支持模型修正相同月份的不同组
    输入：合成跨行绑定
    输出：无
    """
    draft, artifacts = sample()
    draft["blocks"][0]["values"]["count"]["row"] = 1
    with pytest.raises(DataError) as caught:
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert caught.value.code == "REPORT_ROW_BINDING_MISMATCH"
    assert caught.value.details["references"]["carrier"]["row"] == 0
    assert caught.value.details["references"]["count"]["row"] == 1
    assert "月份" in caught.value.details["instruction"]


def test_count_phrase_rejects_identity_without_row_bindings():
    """数量位置必须引用数值，即使模型省略同行绑定。"""
    draft, artifacts = sample()
    block = draft["blocks"][0]
    ref = block["values"]["carrier"]
    block.update(text_template="共 {{total}} 个代码", values={"total": ref}, row_bindings=[])
    with pytest.raises(DataError, match="REPORT_VALUE_TYPE"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    ref["column"] = "count"
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "共 120 个代码"


@pytest.mark.parametrize("suffix", [" 班（按百班计）", " 个百分点", "%"])
def test_percent_reference_rejects_conflicting_unit(suffix):
    """带百分号的冻结值不能再接计数或百分点单位。"""
    draft, artifacts = sample()
    block = draft["blocks"][0]
    artifacts["qa_test"]["artifact"]["columns"][2]["name"] = "rate_pct"
    artifacts["qa_test"]["rows"][0][2] = 12
    ref = dict(block["values"]["count"], column="rate_pct")
    block.update(text_template="{{rate}}" + suffix, values={"rate": ref}, row_bindings=[])
    with pytest.raises(DataError, match="REPORT_DUPLICATE_PERCENT"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})


def test_days_with_events_cannot_be_used_as_flight_count():
    """即使两个单元格都是整数，天数也不能当作航班数。"""
    draft, artifacts = sample()
    block = draft["blocks"][0]
    artifacts["qa_test"]["artifact"]["columns"][2]["name"] = "days_with_events"
    ref = dict(block["values"]["count"], column="days_with_events")
    block.update(text_template="共 {{count}} 班", values={"count": ref}, row_bindings=[])
    with pytest.raises(DataError, match="REPORT_VALUE_TYPE"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    block["text_template"] = "共 {{count}} 天"
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "共 120 天"


def test_explicit_per_hundred_has_one_unit():
    """明确每百班时按同一冻结值展示，百分比后缀只出现在率的表达中。"""
    draft, artifacts = sample()
    block = draft["blocks"][0]
    artifacts["qa_test"]["artifact"]["columns"][2]["name"] = "rate_pct"
    artifacts["qa_test"]["rows"][0][2] = 4.71
    ref = dict(block["values"]["count"], column="rate_pct")
    block.update(
        text_template="每百班取消 {{rate}} 班（取消率 {{rate}}）",
        values={"rate": ref},
        row_bindings=[],
    )
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "每百班取消 4.71 班（取消率 4.71%）"
    block["text_template"] = "每百班取消数 {{rate}}。"
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "每百班取消数 4.71 班。"


def test_top_subset_literal_needs_frozen_count():
    """子集内的计数必须由冻结列填入，排名范围中的数字可保留。"""
    draft, artifacts = sample()
    block = draft["blocks"][0]
    block.update(text_template="前10条中7条来自同一地点", values={}, row_bindings=[])
    with pytest.raises(DataError, match="REPORT_SUBSET_COUNT_UNBOUND"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    block.update(
        text_template="前10条中{{n}}条来自同一地点",
        values={"n": {"artifact_id": "qa_test", "row": 0, "column": "count"}},
    )
    artifacts["qa_test"]["rows"][0][2] = 8
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "前10条中8条来自同一地点"


def test_difference_cannot_reuse_an_operand():
    """差值不能绑定为已展示的原值，独立差值列仍可引用。"""
    draft, artifacts = sample()
    data = artifacts["qa_test"]
    data["artifact"]["columns"].append({"name": "delta", "type": "BIGINT"})
    for row in data["rows"]:
        row.append(80)
    left = {"artifact_id": "qa_test", "row": 0, "column": "count"}
    right = dict(left, row=1)
    block = draft["blocks"][0]
    block.update(
        text_template="两组为{{left}}班与{{right}}班，相差{{delta}}班",
        values={"left": left, "right": right, "delta": left},
        row_bindings=[],
    )
    with pytest.raises(DataError, match="REPORT_DIFFERENCE_REF"):
        render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    block["values"]["delta"] = dict(left, column="delta")
    blocks, _ = render_blocks(ReportDraft.model_validate(draft), artifacts, {})
    assert "".join(s["text"] for s in blocks[0]["segments"]) == "两组为120班与200班，相差80班"
