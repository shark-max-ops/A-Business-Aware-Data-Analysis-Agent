"""发现行选择走真实授权、冻结和报告取值，不借助模型手写模板。"""

import json

import pytest
import test_agent
from metric_pilot.agent.context import compact_group
from metric_pilot.agent.contracts import ToolCall
from metric_pilot.agent.investigations import InvestigationStore
from test_agent import calls, final_from_query
from test_history_context import registry_for
from test_investigations import enqueue, investigation, run_steps
from test_reports import service

agent_workspace = test_agent.agent_workspace
SQL = """SELECT flight_date AS day, COUNT(*) AS record_count,
CAST(NULL AS DOUBLE) AS optional_value FROM v_flights GROUP BY flight_date ORDER BY flight_date"""


def prepared(workspace, sql=SQL):
    """
    作用：建立真实查询及运行中的隔离调查
    输入：合成工作区和可选受控查询
    输出：工具注册表、产物和完整行
    """
    run = enqueue(workspace, investigation(workspace))
    workspace[1].claim()
    registry = registry_for(workspace, run)
    result = registry.invoke(ToolCall(id="query", name="query_sql", arguments={"sql": sql}))
    assert result.ok, result
    ident = result.artifact_ids[0]
    page = registry.invoke(
        ToolCall(id="read", name="read_artifact", arguments={"artifact_id": ident, "limit": 100})
    )
    assert page.ok
    return registry, ident, page.data["rows"]


def test_model_schema_exposes_rows_instead_of_template_choreography(agent_workspace):
    """
    作用：真实模型只接收简单行选择，不再被要求同时填写三份相互依赖的键
    输入：真实工具注册表
    输出：无
    """
    registry, _, _ = prepared(agent_workspace)
    spec = next(s for s in registry.schemas() if s["function"]["name"] == "record_finding")
    properties = spec["function"]["parameters"]["properties"]
    assert "observations" in properties and "note" in properties
    assert "statement" not in properties
    assert not {"values", "row_bindings", "text_template"} & set(properties)


def test_separate_rows_and_nulls_are_bound_and_rendered_by_server(agent_workspace):
    """
    作用：跨行比较分别生成正确绑定，空数值保留缺失而不虚构为零
    输入：真实日分组结果的首尾行
    输出：无
    """
    registry, ident, rows = prepared(agent_workspace)
    assert len(rows) > 1
    indices = [0, len(rows) - 1]
    result = registry.invoke(
        ToolCall(
            id="finding",
            name="record_finding",
            arguments={
                "kind": "fact",
                "check": "核对首尾日期与样本数量",
                "observations": [
                    {
                        "artifact_id": ident,
                        "row": index,
                        "columns": ["day", "record_count", "optional_value"],
                    }
                    for index in indices
                ],
            },
        )
    )
    assert result.ok, result
    block = result.data["report_block"]
    assert "无可用值" in result.data["text"]
    assert len(block["values"]) == 6 and len(block["row_bindings"]) == 2
    for binding, expected in zip(block["row_bindings"], indices, strict=True):
        assert {
            block["values"][key]["row"] for key in binding["dimensions"] + binding["measures"]
        } == {expected}
    group = [{"role": "tool", "tool_call_id": "finding", "content": result.model_dump_json()}]
    compact = json.loads(compact_group(group)[0]["content"])["data"]
    assert compact["report_block"] == block and "value_refs" not in compact
    assert result.data["value_refs"] == block["values"]


@pytest.mark.parametrize(
    "override,code",
    [
        ({"row": 999999}, "REPORT_ROW_NOT_FOUND"),
        ({"columns": ["unknown"]}, "REPORT_COLUMN_NOT_FOUND"),
        ({"artifact_id": "qa_other_investigation"}, "ARTIFACT_NOT_FOUND"),
        ({"columns": ["day", "day"]}, "INVALID_ARGUMENT"),
        ({"row": True}, "INVALID_ARGUMENT"),
    ],
)
def test_invalid_selection_does_not_save_or_substitute_rows(agent_workspace, override, code):
    """
    作用：错误行列、归属和参数仍被拒绝，不能偷偷选择其他值或保存部分发现
    输入：真实产物及无效行选择
    输出：无
    """
    registry, ident, _ = prepared(agent_workspace)
    result = registry.invoke(
        ToolCall(
            id="bad",
            name="record_finding",
            arguments={
                "kind": "fact",
                "check": "拒绝无效选择",
                "observations": [{"artifact_id": ident, "row": 0, "columns": ["day"], **override}],
            },
        )
    )
    assert not result.ok and result.error_code == code, result
    assert not InvestigationStore(registry.store.engine).read(registry.run["investigation_id"])[
        "findings"
    ]


def test_largest_selection_fits_existing_reference_contract(agent_workspace):
    """
    作用：最大四行六列输入恰好生成二十四个引用，不再由模型计算模板容量
    输入：真实聚合产物与四份行选择
    输出：无
    """
    sql = (
        "SELECT COUNT(*) AS a, COUNT(*) AS b, COUNT(*) AS c, COUNT(*) AS d, "
        "COUNT(*) AS e, COUNT(*) AS f FROM v_flights"
    )
    registry, ident, _ = prepared(agent_workspace, sql)
    result = registry.invoke(
        ToolCall(
            id="max",
            name="record_finding",
            arguments={
                "kind": "fact",
                "check": "核对引用容量边界",
                "observations": [{"artifact_id": ident, "row": 0, "columns": list("abcdef")}] * 4,
            },
        )
    )
    assert result.ok and len(result.data["report_block"]["values"]) == 24, result


def test_source_free_limitation_is_explicitly_unverified(agent_workspace):
    """
    作用：无可用数据时仍能保存真实限制，不要求伪造占位值
    输入：无引用的限制说明
    输出：无
    """
    registry, _, _ = prepared(agent_workspace)
    result = registry.invoke(
        ToolCall(
            id="limitation",
            name="record_finding",
            arguments={
                "kind": "limitation",
                "check": "记录尚未核查的原因",
                "note": "尚未核对机场构成",
            },
        )
    )
    assert result.ok and result.data["text"].startswith("待复核说明：")
    assert not result.data["report_block"].get("values")
    assert result.data["verification"] == "unverified"


def test_generated_block_survives_real_runtime_and_frozen_report(agent_workspace):
    """
    作用：行选择经正式运行保存后，服务端文字块可直接提交冻结报告
    输入：隔离模型选择与真实SQL、发现和报告服务
    输出：无
    """

    def observe(messages):
        """作用：只选真实查询行；输入：工具上下文；输出：简短发现调用。"""
        ident = final_from_query(messages)["artifact_ids"][0]
        return calls(
            (
                "record_finding",
                {
                    "kind": "fact",
                    "check": "核对两个日期的样本数",
                    "observations": [
                        {"artifact_id": ident, "row": row, "columns": ["day", "record_count"]}
                        for row in (0, 1)
                    ],
                },
            )
        )

    def report(messages):
        """作用：直接复用服务端规范块；输入：真实发现结果；输出：报告候选。"""
        block = next(
            json.loads(m["content"])["data"]["report_block"]
            for m in reversed(messages)
            if m["role"] == "tool" and "report_block" in json.loads(m["content"]).get("data", {})
        )
        return {
            "kind": "report",
            "report": {
                "schema_version": "1",
                "title": "合成行选择验收",
                "scope": "固定合成日期范围",
                "metric_revision_ids": [],
                "blocks": [block],
                "limitations": ["合成测试数据"],
            },
        }

    run, _ = run_steps(
        agent_workspace,
        investigation(agent_workspace),
        [calls(("query_sql", {"sql": SQL})), observe, report],
    )
    assert run["status"] == "completed", run
    saved = service(agent_workspace).read(run["result"]["report_id"], run["result"]["revision_id"])
    block = saved.blocks[0]
    assert len([segment for segment in block.segments if segment.value]) == 4
    events = agent_workspace[1].events_after(run["id"], 0)
    assert all(e["payload"]["ok"] for e in events if e["type"] == "tool.finished")


def test_query_argument_feedback_explains_cross_field_rule_without_leaking_input(agent_workspace):
    """
    作用：真实复测中的正式查询混带探索说明时返回可修复规则，不再误报文字引用建议
    输入：隔离正式查询参数与敏感示例说明
    输出：无
    """
    registry, _, _ = prepared(agent_workspace)
    result = registry.invoke(
        ToolCall(
            id="bad_scope",
            name="query_sql",
            arguments={
                "sql": SQL,
                "purpose": "reviewed_metric",
                "metric_revision_ids": [agent_workspace[3]["id"]],
                "exploration_scope": {
                    key: "private-test-marker"
                    for key in ("population", "calculation", "missing_values")
                },
            },
        )
    )
    assert not result.ok and result.error_code == "INVALID_ARGUMENT"
    assert "exploration_scope须省略或为null" in result.data["instruction"]
    assert "不删除指标" in result.data["instruction"]
    assert "private-test-marker" not in result.model_dump_json()
