"""探索与正式指标权限边界；模型隔离，查询及报告使用真实服务。"""

import copy
import json
import threading
import time

import pytest
import test_agent
from metric_pilot.agent.context import compact_group, fit_context, initial_messages
from metric_pilot.agent.contracts import SQLArgs, ToolCall
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import RunContext
from metric_pilot.reports.contracts import ReportDraft, ReportRevisionWrite
from pydantic import ValidationError
from test_agent import calls, execute, final_from_query
from test_investigations import enqueue, investigation
from test_reports import SQL, report_candidate, service

agent_workspace = test_agent.agent_workspace
SCOPE = {
    "population": "合成快照全部记录，按航班日期分组，不排除取消或备降",
    "calculation": "逐日记录数与取消条数占当日全部记录的比例；不是正式项目取消率",
    "missing_values": "COUNT 包含全部记录；取消标记仅计真值，不推断未知原因",
}


def test_brief_context_deduplicates_only_identical_validated_finding_refs():
    """
    作用：紧凑上下文只合并重复引用，输入映射与原始审计保持完整
    输入：合成发现的配对消息和服务端结果
    输出：无
    """
    refs = {"n": {"artifact_id": "qa_synthetic", "row": 0, "column": "n"}}
    group = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "record",
                    "function": {
                        "name": "record_finding",
                        "arguments": json.dumps({"statement": {"values": refs}}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "record",
            "content": json.dumps(
                {
                    "ok": True,
                    "data": {"id": "finding_synthetic", "text": "合成核查", "value_refs": refs},
                }
            ),
        },
    ]
    original = copy.deepcopy(group)
    compact = compact_group(group, brief=True)
    assert "value_refs" not in json.loads(compact[-1]["content"])["data"]
    assert (
        json.loads(compact[0]["tool_calls"][0]["function"]["arguments"])["statement"]["values"]
        == refs
    )
    assert group == original
    altered = json.loads(group[-1]["content"])
    altered["data"]["value_refs"]["n"]["row"] = 1
    group[-1]["content"] = json.dumps(altered)
    assert (
        json.loads(compact_group(group, brief=True)[-1]["content"])["data"]["value_refs"]["n"][
            "row"
        ]
        == 1
    )


def exploration(sql=SQL, **overrides):
    """
    作用：构造有完整声明的探索调用
    输入：SQL 及参数覆盖
    输出：测试工具调用
    """
    return (
        "query_sql",
        {"sql": sql, "purpose": "exploratory", "exploration_scope": SCOPE, **overrides},
    )


def test_failed_metric_then_rejected_default_can_recover_as_exploration(agent_workspace):
    """
    作用：误解析指标后允许显式探索，冻结范围并防止报告升级审核身份
    输入：隔离快照与真实查询执行器
    输出：无
    """
    run, _ = execute(
        agent_workspace,
        [
            calls(("resolve_metric", {"metric_key": "missing"})),
            calls(("query_sql", {"sql": SQL})),
            calls(exploration()),
            report_candidate,
        ],
    )
    assert run["status"] == "completed" and run["result"]["kind"] == "report", run
    events = agent_workspace[1].events_after(run["id"], 0)
    results = [e["payload"] for e in events if e["type"] == "tool.finished"]
    assert results[1]["error_code"] == "METRIC_REVIEW_OR_CLARIFICATION_REQUIRED"
    assert results[2]["ok"]
    artifact = results[2]["data"]["artifact"]
    assert artifact["query_purpose"] == "exploratory"
    assert artifact["exploration_scope"] == SCOPE
    assert artifact["metric_revision_ids"] == []
    assert artifact["unavailable_metric_keys"] == ["missing"]
    reports = service(agent_workspace)
    result = run["result"]
    report = reports.read(result["report_id"], result["revision_id"])
    evidence = reports.evidence(report.report_id, report.revision_id, "count")
    assert evidence.artifact.exploration_scope.model_dump() == SCOPE
    assert evidence.metrics == []
    assert any("包含数据探索结果" in note for note in report.limitations)

    # 人工删改说明也不能删掉服务端根据固定产物生成的探索边界。
    draft = report.draft.model_copy(deep=True)
    draft.limitations = []
    revised = reports.revise(
        report.report_id, ReportRevisionWrite(expected_version=1, report=draft)
    )
    assert any("包含数据探索结果" in note for note in revised.limitations)
    forged = copy.deepcopy(draft.model_dump())
    forged["metric_revision_ids"] = [agent_workspace[3]["id"]]
    with pytest.raises(DataError, match="REPORT_METRIC_BINDING"):
        reports.prepare(run, ReportDraft.model_validate(forged))


def test_unavailable_metric_does_not_block_another_approved_metric(agent_workspace):
    """
    作用：不可用指标只限制自身，不封锁已正确解析的其他指标
    输入：隔离库中已审核且日期适用的测试指标
    输出：无
    """
    ident = agent_workspace[3]["id"]
    run, _ = execute(
        agent_workspace,
        [
            calls(("resolve_metric", {"metric_key": "missing"})),
            calls(("resolve_metric", {"metric_key": "arrival_delay_rate"})),
            calls(
                (
                    "query_sql",
                    {"sql": SQL, "purpose": "reviewed_metric", "metric_revision_ids": [ident]},
                )
            ),
            final_from_query,
        ],
    )
    assert run["status"] == "completed", run
    artifact = next(
        e["payload"]["data"]["artifact"]
        for e in agent_workspace[1].events_after(run["id"], 0)
        if e["type"] == "tool.finished" and e["payload"].get("artifact_ids")
    )
    assert artifact["query_purpose"] == "reviewed_metric"
    assert artifact["metric_revision_ids"] == [ident]


@pytest.mark.parametrize(
    "changes",
    [
        {"exploration_scope": None},
        {"exploration_scope": {**SCOPE, "population": "   "}},
        {"metric_revision_ids": ["unreviewed"]},
        {"purpose": "reviewed_metric", "exploration_scope": None},
        {"purpose": "standard"},
    ],
)
def test_exploration_contract_rejects_ambiguous_or_forged_identity(changes):
    """
    作用：拒绝缺范围探索、空白说明、混绑指标与无修订的正式计算
    输入：无效参数组合
    输出：无
    """
    with pytest.raises(ValidationError):
        SQLArgs.model_validate(exploration(**changes)[1])


def test_exploration_still_uses_sql_guard_and_failure_has_no_artifact(agent_workspace):
    """
    作用：探索用途不能取得数据库外部文件或产生失败证据
    输入：禁止的外部文件读取 SQL
    输出：无
    """
    run, _ = execute(
        agent_workspace,
        [
            calls(exploration("SELECT * FROM read_csv('private.csv')")),
            {
                "kind": "clarification",
                "question": "当前查询不被允许",
                "missing_requirements": ["可用表"],
            },
        ],
    )
    assert run["status"] == "completed"
    results = [
        e["payload"]
        for e in agent_workspace[1].events_after(run["id"], 0)
        if e["type"] == "tool.finished"
    ]
    assert not results[0]["ok"] and not results[0]["artifact_ids"]


def test_optional_function_rejection_explains_limit_without_losing_success(agent_workspace):
    """
    作用：拒绝未开放统计函数时反馈具体限制，已有探索仍可作为报告证据
    输入：成功聚合和受限排名函数
    输出：无
    """
    run, _ = execute(
        agent_workspace,
        [
            calls(exploration()),
            calls(
                exploration(
                    "SELECT ROW_NUMBER() OVER (ORDER BY flight_date) AS rank FROM v_flights"
                )
            ),
            report_candidate,
        ],
    )
    assert run["status"] == "completed", run
    results = [
        e["payload"]
        for e in agent_workspace[1].events_after(run["id"], 0)
        if e["type"] == "tool.finished"
    ]
    failed = results[-1]
    assert failed["error_code"] == "FUNCTION_NOT_ALLOWED" and not failed["artifact_ids"]
    assert failed["data"]["error_details"]["function"] == "ROW_NUMBER"
    assert "ROW_NUMBER" not in failed["data"]["error_details"]["allowed_functions"]
    assert run["result"]["kind"] == "report"


def test_wide_exploration_batch_preserves_scope_within_real_context_budget(agent_workspace):
    """
    作用：完整字段结构和多项质量统计同批返回时仍保留最新结果与范围
    输入：实际调查预算、完整合成结构及真实聚合结果
    输出：无
    """
    config = agent_workspace[0]
    run = enqueue(agent_workspace, investigation(agent_workspace))
    registry = ToolRegistry(
        run,
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        service(agent_workspace).query,
        config,
        threading.Event(),
    )
    registry.invoke(
        ToolCall(id="structure", name="describe_table", arguments={"table_name": "v_flights"})
    )
    # 组合多个真实字段质量计数，覆盖宽结果和长 SQL 在上下文中的重复开销。
    checks = [
        "COUNT(*) AS total_records",
        "SUM(CASE WHEN cancelled=1 THEN 1 ELSE 0 END) AS cancelled_records",
        "SUM(CASE WHEN diverted=1 THEN 1 ELSE 0 END) AS diverted_records",
        *[
            f"SUM(CASE WHEN cancelled=0 AND diverted=0 AND {condition} THEN 1 ELSE 0 END) AS {name}"
            for condition, name in [
                ("arrival_delay IS NULL", "operated_missing_arrival_delay"),
                ("arrival_delay IS NOT NULL", "operated_with_arrival_delay"),
                ("arrival_delay_nonnegative IS NULL", "operated_missing_nonnegative_delay"),
                ("arrival_delayed_15 IS NULL", "operated_missing_delayed_flag"),
                ("arrival_delay < 0", "operated_early_arrival"),
                ("arrival_delay = 0", "operated_exactly_on_time"),
                ("arrival_delay > 0", "operated_positive_arrival_delay"),
                ("arrival_delay >= 15", "operated_at_least_fifteen_minutes"),
            ]
        ],
    ]
    sql = "SELECT " + ", ".join(checks) + " FROM v_flights"
    scope = {
        "population": "取消、备降以全表为参照；到达偏差仅检查未取消未备降记录。",
        "calculation": "分别计数全部、取消、备降、缺失、提前、零偏差及正偏差记录，不套项目延误率。",
        "missing_values": "到达偏差缺失单列，不当成零；提前到达不改成非负延误；标记缺失单列。",
    }
    registry.invoke(ToolCall(id="previous", name="query_sql", arguments=exploration(sql)[1]))
    batch = [
        ToolCall(id="source", name="search_knowledge", arguments={"query": "ArrDelay"}),
        ToolCall(
            id="quality", name="query_sql", arguments=exploration(sql, exploration_scope=scope)[1]
        ),
    ]
    group = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in batch
            ],
        }
    ]
    for call in batch:
        result = registry.invoke(call)
        assert result.ok, result
        group.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result.model_dump(), ensure_ascii=False),
            }
        )
    original = copy.deepcopy(group)
    available = (
        32000 - 3000 - len(json.dumps(registry.schemas(), ensure_ascii=False).encode()) - 1024
    )
    messages = fit_context(initial_messages(run, registry, []), [group], registry, available)
    assert {m["tool_call_id"] for m in messages if m["role"] == "tool"} == {"source", "quality"}
    artifact = json.loads(messages[-1]["content"])["data"]["artifact"]
    assert artifact["exploration_scope"] == scope
    assert artifact["query_purpose"] == "exploratory"
    assert group == original

    # 合成较长资料摘录触发紧凑模式；只缩短展示，不能删工具回复或探索范围。
    stress = copy.deepcopy(group)
    source_result = json.loads(stress[1]["content"])
    source_result["data"]["items"][0]["excerpt"] = "合成资料摘录，用于上下文边界测试。" * 30
    stress[1]["content"] = json.dumps(source_result, ensure_ascii=False)
    full = fit_context(initial_messages(run, registry, []), [stress], registry, 100000)
    tighter = len(json.dumps(full, ensure_ascii=False).encode()) - 200
    compact = fit_context(initial_messages(run, registry, []), [stress], registry, tighter)
    assert len(json.dumps(compact, ensure_ascii=False).encode()) <= tighter
    assert {m["tool_call_id"] for m in compact if m["role"] == "tool"} == {"source", "quality"}
    assert json.loads(compact[-1]["content"])["data"]["artifact"]["exploration_scope"] == scope
    cached_before = copy.deepcopy(registry.query_summaries)
    room = len(json.dumps(full, ensure_ascii=False).encode()) - 1200
    compact = fit_context(initial_messages(run, registry, []), [stress], registry, room)
    memory = next(
        json.loads(m["content"])
        for m in compact
        if m["role"] == "system" and '"recent_successful_queries"' in m["content"]
    )
    assert len(json.dumps(compact, ensure_ascii=False).encode()) <= room
    assert [item["artifact_id"] for item in memory["recent_successful_queries"]] == [
        item["artifact_id"] for item in cached_before
    ]
    assert registry.query_summaries == cached_before
    assert json.loads(compact[-1]["content"])["data"]["artifact"]["exploration_scope"] == scope
