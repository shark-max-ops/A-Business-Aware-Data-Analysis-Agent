import json

import pytest
from metric_pilot.agent.context import compact_group
from metric_pilot.agent.contracts import ReadInvestigation, ToolCall
from metric_pilot.agent.history import match_score
from pydantic import ValidationError
from test_history_context import agent_workspace as agent_workspace
from test_history_context import history, registry_for
from test_investigations import enqueue


@pytest.mark.parametrize(
    "args",
    [
        {"query": " "},
        {"query": "OH", "finding_id": "x"},
        {"query": "OH", "offset": 1},
        {"finding_id": "x", "limit": 1},
    ],
)
def test_conflicts(args):
    with pytest.raises(ValidationError):
        ReadInvestigation.model_validate(args)


def test_matching():
    assert match_score("取消原因", "四月取消原因构成")
    assert match_score("取消原因", "取消情况及原因")
    assert match_score("ＯＨ", "OH April")
    assert not match_score("OH", "OO OHO")
    assert not match_score("2026-01", "2026-02")


def test_query_trim_before_length_check():
    assert ReadInvestigation(query=" " * 210 + "OH" + " " * 210).query == "OH"


def test_excerpt_contains_tail_match_and_original_text():
    from metric_pilot.agent.history import matching_excerpt

    text = "背景" * 300 + "ＯＨ取消原因" + "后续" * 100
    excerpt = matching_excerpt("OH", {"text": text})
    assert "ＯＨ" in excerpt and "text:" in excerpt and "…" in excerpt


def test_reply_capacity_preserves_candidate_identity():
    from metric_pilot.agent.history import bound_search_reply

    item = {
        "id": "a",
        "verification": "frozen_query_only",
        "columns": ["x" * 10000],
        "source_run_status": "completed",
        "excerpt": "x" * 500,
    }
    result = bound_search_reply({"candidates": [item], "matched_count": 1})
    assert result["returned_count"] == 1
    assert result["candidates"][0]["id"] == "a"
    assert result["omitted"] and result["omission_reasons"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 4096


def test_reply_shortening_keeps_matched_text():
    from metric_pilot.agent.history import bound_search_reply, matching_excerpt

    item = {
        "id": "a",
        "columns": ["x" * 9000],
        "excerpt": matching_excerpt("OH", {"exploration_scope": "padding " * 90 + "OH tail"}),
    }
    reply = bound_search_reply({"query": "OH", "candidates": [item], "matched_count": 1})
    assert "OH" in reply["candidates"][0]["excerpt"]


@pytest.mark.parametrize(
    "arguments,category",
    [
        ({"query": " "}, "EMPTY_QUERY"),
        ({"query": "OH", "finding_id": "x"}, "CONFLICTING_READ_MODES"),
        ({"query": "OH", "offset": 1}, "READ_PAGINATION_CONFLICT"),
    ],
)
def test_actionable_search_errors(agent_workspace, arguments, category):
    from test_investigations import investigation

    agent_workspace[0].run_history_compaction = True
    registry = registry_for(
        agent_workspace, enqueue(agent_workspace, investigation(agent_workspace))
    )
    result = registry.invoke(ToolCall(id="bad", name="read_investigation", arguments=arguments))
    assert result.error_code == "INVALID_ARGUMENT"
    assert result.data["category"] == category
    assert result.data["instruction"]


def test_search_and_direct_read_preserve_authorization(agent_workspace):
    inv, _, artifact, _ = history(agent_workspace)
    agent_workspace[0].run_history_compaction = True
    run = enqueue(agent_workspace, inv)
    registry = registry_for(agent_workspace, run)
    result = registry.invoke(
        ToolCall(id="s", name="read_investigation", arguments={"query": "冻结计数"})
    )
    assert result.ok and result.data["candidates"]
    candidate = result.data["candidates"][0]
    assert candidate["type"] == "finding"
    assert candidate["artifacts"] == [
        {"artifact_id": artifact, "readable": "requires_read_artifact_validation"}
    ]
    group = [{"role": "tool", "tool_call_id": "s", "content": result.model_dump_json()}]
    assert json.loads(compact_group(group, brief=True)[0]["content"])["data"] == result.data
    result = registry.invoke(
        ToolCall(id="d", name="read_investigation", arguments={"finding_id": candidate["id"]})
    )
    assert result.ok and result.data["findings"][0]["value_refs"]
    assert registry.invoke(
        ToolCall(id="a", name="read_artifact", arguments={"artifact_id": artifact})
    ).ok


def test_navigation_deduplicates_and_bounds():
    from metric_pilot.agent.history import navigation

    items = [{"artifact_id": "a", "columns": ["x"], "query_purpose": "standard"}]
    nav = navigation(items + items, [], [])
    assert len(nav["items"]) == 1
    assert len(json.dumps(nav, ensure_ascii=False).encode()) <= 1536
    assert "query" in nav["note"]


def test_navigation_reclaimed_items_keep_original_priority():
    from metric_pilot.agent.history import navigation

    current = [
        {"artifact_id": "older", "columns": ["short"]},
        {"artifact_id": "middle", "columns": ["col_" + "x" * 450]},
        {"artifact_id": "newest", "columns": ["col_" + "y" * 450]},
    ]
    result = navigation(current, [], [])
    assert [i["artifact_id"] for i in result["items"]] == ["newest", "middle", "older"]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 1536


def test_navigation_prioritizes_latest_and_keeps_correction_boundary():
    from metric_pilot.agent.history import navigation

    nav = navigation(
        [
            {"artifact_id": "old", "columns": ["x" * 9000]},
            {"artifact_id": "new", "columns": ["y" * 9000]},
        ],
        [
            {
                "id": "f",
                "direct_successor_id": "g",
                "effective_successor_id": "h",
                "source_run_status": "failed",
            }
        ],
        [],
    )
    assert nav["items"][0]["artifact_id"] == "new"
    assert next(i for i in nav["items"] if i.get("id") == "f")["effective_successor_id"] == "h"


def test_complete_cache_survives_old_group_eviction(agent_workspace):
    from metric_pilot.agent.context import fit_context, initial_messages
    from test_investigations import investigation

    agent_workspace[0].run_history_compaction = True
    run = enqueue(agent_workspace, investigation(agent_workspace))
    registry = registry_for(agent_workspace, run)
    registry.query_summaries = [
        {
            "artifact_id": "a",
            "rows": 1,
            "truncated": False,
            "result_preview": {"columns": ["n"], "rows": [[42]], "complete": True},
        },
        {
            "artifact_id": "partial",
            "rows": 2,
            "truncated": False,
            "result_preview": {"columns": ["n"], "rows": [[1]], "complete": False},
        },
    ]
    messages = fit_context(initial_messages(run, registry, []), [], registry, 27000)
    memory = json.loads(messages[-1]["content"])
    assert [s["artifact_id"] for s in memory["recent_successful_queries"]] == ["a"]
    assert memory["recent_successful_queries"][0]["result_preview"]["row_indices"] == [0]


def test_read_artifact_keeps_navigation_scope(agent_workspace):
    from test_investigations import investigation
    from test_reports import SQL

    agent_workspace[0].run_history_compaction = True
    registry = registry_for(
        agent_workspace, enqueue(agent_workspace, investigation(agent_workspace))
    )
    result = registry.invoke(ToolCall(id="q", name="query_sql", arguments={"sql": SQL}))
    assert result.ok
    ident = result.artifact_ids[0]
    before = registry.query_summaries[-1]
    assert registry.invoke(
        ToolCall(id="r", name="read_artifact", arguments={"artifact_id": ident})
    ).ok
    after = registry.query_summaries[-1]
    for key in ("columns", "date_range", "metric_revision_ids", "verification"):
        assert key in after and after[key] == before[key]


def test_disabled_registry_rejects_search(agent_workspace):
    from test_investigations import investigation

    run = enqueue(agent_workspace, investigation(agent_workspace))
    registry = registry_for(agent_workspace, run)
    assert not registry.history_enabled
    assert (
        registry.invoke(
            ToolCall(id="s", name="read_investigation", arguments={"query": "OH"})
        ).error_code
        == "INVALID_ARGUMENT"
    )


def test_correction_chain_keeps_current_locator():
    from metric_pilot.agent.history import corrected_findings

    records = corrected_findings(
        [
            {"id": "a", "supersedes": None},
            {"id": "b", "supersedes": "a"},
            {"id": "c", "supersedes": "b"},
        ]
    )
    assert records[0]["superseded"]
    assert records[0]["direct_successor_id"] == "b"
    assert records[0]["effective_successor_id"] == "c"
