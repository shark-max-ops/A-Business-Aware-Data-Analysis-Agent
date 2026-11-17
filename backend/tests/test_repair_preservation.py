import pytest
from metric_pilot.agent.report_repair import RepairGuard
from metric_pilot.data.common import DataError
from test_reports import agent_workspace as agent_workspace


def test_repair_guard_rejects_lost_or_changed_blocks():
    guard = RepairGuard()
    guard.blocks = [{"id": "month", "type": "table", "artifact_id": "a"}]
    guard.required = ["month"]
    guard.fields = {"limitations": ["incomplete"]}
    with pytest.raises(DataError, match="REPORT_REPAIR_CONTENT_LOSS"):
        guard.check({"blocks": [], "limitations": ["incomplete"]})
    with pytest.raises(DataError):
        guard.check({"blocks": [dict(guard.blocks[0], artifact_id="b")], "limitations": []})
    guard.check({"blocks": guard.blocks, "limitations": ["incomplete"]})


def test_execution_issues_preserve_failed_tools_without_authorizing():
    import json

    from metric_pilot.agent.compaction import execution_issues

    group = [
        {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": "query_sql"}}]},
        {
            "role": "tool",
            "tool_call_id": "t",
            "content": json.dumps({"ok": False, "error_code": "FUNCTION_NOT_ALLOWED"}),
        },
    ]
    issues = execution_issues([group], [])
    assert issues == [
        {
            "tool": "query_sql",
            "error_code": "FUNCTION_NOT_ALLOWED",
            "count": 1,
            "later_success": False,
        }
    ]
    assert execution_issues([], issues) == issues


def test_numeric_version_repair_keeps_original_real_blocks(agent_workspace):
    import copy

    from test_agent import calls, execute
    from test_reports import SQL, report_candidate

    captured = {}

    def invalid(messages):
        candidate = report_candidate(messages)
        candidate["report"]["schema_version"] = 1
        captured["candidate"] = copy.deepcopy(candidate)
        return candidate

    def repaired(messages):
        import json

        feedback = json.loads(messages[-1]["content"])
        assert feedback["valid_table_draft"] is None
        assert "table" in feedback["required_block_ids"]
        original = json.loads(messages[-2]["content"])
        assert original["report"]["schema_version"] == 1
        original["report"]["schema_version"] = "1"
        return original

    run, _ = execute(agent_workspace, [calls(("query_sql", {"sql": SQL})), invalid, repaired])
    assert run["status"] == "completed", run
    assert not run["result"].get("partial")


def test_content_loss_exhaustion_retains_original_blocks_as_partial(agent_workspace):
    import copy

    from test_agent import calls, execute
    from test_reports import SQL, report_candidate, service

    def invalid(messages):
        candidate = report_candidate(messages)
        candidate["report"]["schema_version"] = 1
        return candidate

    def dropped(messages):
        import json

        candidate = copy.deepcopy(json.loads(messages[-2]["content"]))
        candidate["report"]["schema_version"] = "1"
        candidate["report"]["blocks"] = candidate["report"]["blocks"][:1]
        return candidate

    run, _ = execute(
        agent_workspace, [calls(("query_sql", {"sql": SQL})), invalid, dropped, dropped]
    )
    assert run["status"] == "completed", run
    assert run["result"]["partial"]
    frozen = service(agent_workspace).read(run["result"]["report_id"], run["result"]["revision_id"])
    assert {b.id for b in frozen.blocks} == {"count", "pct", "note", "table", "chart"}


def test_nested_thinking_fields_never_enter_repair_projection():
    from metric_pilot.agent.report_repair import safe_structure

    assert safe_structure({"blocks": [{"id": "x", "reasoning_content": "private"}]}) == {
        "blocks": [{"id": "x"}]
    }


def test_issue_metadata_counts_against_original_size_limit():
    from metric_pilot.agent.compaction import validate_summary

    with pytest.raises(DataError, match="COMPACT_SUMMARY_TOO_LARGE"):
        validate_summary(
            {"background": "ok", "source_ids": [], "gaps": []},
            {},
            issues=[{"error_code": "X" * 2600}],
        )


def test_summary_diagnostic_keeps_fixed_classification_and_no_error_body():
    from types import SimpleNamespace

    from metric_pilot.agent.compaction import summary_diagnostic

    events = []
    runtime = SimpleNamespace(store=SimpleNamespace(emit=lambda *args: events.append(args)))
    summary_diagnostic(
        runtime,
        SimpleNamespace(run_id="test"),
        stage="request",
        attempt=1,
        error_code="COMPACT_LOCAL_TIMEOUT",
    )
    details = events[0][2]["compaction"]
    assert details["classification"] == "local_timeout"
    assert details["kind"] == "initial"
    assert "candidate" not in details
