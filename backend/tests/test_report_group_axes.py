"""阶段7：合成分组结果的图表身份校验。"""

import pytest
from metric_pilot.data.common import DataError
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.materialize import render_blocks


@pytest.mark.parametrize("kind", ["bar", "line"])
@pytest.mark.parametrize(
    "keys", [["OH", "OH"], [None, None], [{"type": "date", "value": "2026-01-01"}] * 2]
)
def test_duplicate_group_axis_rejected(kind, keys):
    """
    作用：拒绝分组维度丢失后横轴重复的图表。
    输入：合成横轴与图表类型。
    输出：无。
    """
    draft, artifacts = grouped_chart(kind, keys)
    with pytest.raises(DataError, match="REPORT_CHART_AXIS_AMBIGUOUS"):
        render_blocks(draft, artifacts, {})


@pytest.mark.parametrize("kind", ["bar", "line"])
@pytest.mark.parametrize("keys", [[], ["2026-01/OH", "2026-02/OH"]])
def test_unique_group_axis_preserves_rows(kind, keys):
    """
    作用：确认唯一分组与空结果仍能物化且不改变取值行号。
    输入：合成横轴与图表类型。
    输出：无。
    """
    draft, artifacts = grouped_chart(kind, keys)
    blocks, _ = render_blocks(draft, artifacts, {})
    assert [row[0]["raw"] for row in blocks[0]["rows"]] == keys
    assert [row[1]["value_ref"]["row"] for row in blocks[0]["rows"]] == list(range(len(keys)))


def grouped_chart(kind, keys):
    """
    作用：创建不依赖真实数据或模型的合成图表候选。
    输入：图表类型与横轴值。
    输出：草稿及冻结结果形状的测试数据。
    """
    draft = ReportDraft.model_validate(
        {
            "schema_version": "1",
            "title": "合成分组",
            "scope": "合成数据",
            "blocks": [
                {
                    "type": "chart",
                    "id": "groups",
                    "title": "分组",
                    "artifact_id": "qa_synthetic",
                    "kind": kind,
                    "x": "group",
                    "y": ["count"],
                    "format": "integer",
                }
            ],
        }
    )
    artifacts = {
        "qa_synthetic": {
            "artifact": {
                "ordered": True,
                "columns": [
                    {"name": "group", "type": "VARCHAR"},
                    {"name": "count", "type": "BIGINT"},
                ],
            },
            "rows": [[key, index + 1] for index, key in enumerate(keys)],
        }
    }
    return draft, artifacts
