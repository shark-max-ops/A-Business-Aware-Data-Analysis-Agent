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
