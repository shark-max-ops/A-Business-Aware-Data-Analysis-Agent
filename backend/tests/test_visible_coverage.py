import json
from types import SimpleNamespace

import pytest
from metric_pilot.agent.compaction import summary_request, validate_summary
from metric_pilot.agent.context import compact_group


@pytest.mark.parametrize(
    "indices,total,truncated,state",
    [
        ([0, 1, 2, 3], 4, False, "complete"),
        ([0, 1], 4, False, "partial"),
        ([0, 2, 3], 4, False, "partial"),
        ([], 0, False, "complete"),
        ([0, 1], 2, True, "partial"),
    ],
)
def test_visible_result_coverage(indices, total, truncated, state):
    data = {
        "artifact": {"id": "a", "returned_rows": total, "truncated": truncated},
        "rows": [[i] for i in indices],
        "row_indices": indices,
    }
    group = [
        {"role": "tool", "tool_call_id": "t", "content": json.dumps({"ok": True, "data": data})}
    ]
    projected = json.loads(compact_group(group, SimpleNamespace())[0]["content"])["data"]
    assert projected["visible_coverage"]["state"] == state
    assert projected["visible_coverage"]["row_indices"] == indices
    if state == "complete":
        assert "完整固定值用 read_artifact" not in projected["context_note"]


def test_brief_coverage_uses_only_selected_rows():
    data = {
        "artifact": {"id": "a", "returned_rows": 4, "truncated": False},
        "rows": [[i] for i in range(4)],
    }
    group = [
        {"role": "tool", "tool_call_id": "t", "content": json.dumps({"ok": True, "data": data})}
    ]
    projected = json.loads(compact_group(group, SimpleNamespace(), brief=True)[0]["content"])[
        "data"
    ]
    assert projected["visible_coverage"]["state"] == "partial"
    assert projected["visible_coverage"]["row_indices"] == [0]


def test_summary_selected_prefix_coverage_excludes_tail():
    groups = []
    for ident in ("a", "tail"):
        data = {
            "artifact": {"id": ident, "returned_rows": 4, "truncated": False},
            "rows": [["x" * 800] for _ in range(4)],
        }
        groups.append(
            [
                {
                    "role": "tool",
                    "tool_call_id": ident,
                    "content": json.dumps({"ok": True, "data": data}),
                }
            ]
        )
    from metric_pilot.agent.compaction import SUMMARY_PROMPT
    keep, messages = summary_request({}, groups, None, len(SUMMARY_PROMPT.encode()) + 2000)
    assert keep == 1
    data = json.loads(messages[1]["content"])
    assert set(data["visible_coverage"]) == {"a"}
    assert data["visible_coverage"]["a"][0]["state"] == "partial"


def test_metadata_only_does_not_claim_values():
    data = {"artifact": {"id": "a", "returned_rows": 4, "truncated": False}}
    projected = json.loads(
        compact_group([{"role": "tool", "content": json.dumps({"ok": True, "data": data})}])[0][
            "content"
        ]
    )["data"]
    assert projected["visible_coverage"]["state"] == "metadata_only"


def test_summary_cannot_drop_server_input_coverage():
    coverage = {"a": [{"state": "complete", "row_indices": [0]}]}
    summary = validate_summary(
        {"background": "review", "source_ids": [], "gaps": []}, {}, coverage=coverage
    )
    assert summary["coverage_encoding"] == "row_ranges_v1"
    assert summary["input_coverage"]["a"][0]["row_ranges"] == [[0, 1]]
    assert summary["input_coverage"]["a"][0]["state"] == coverage["a"][0]["state"]
    assert coverage["a"][0]["row_indices"] == [0]
