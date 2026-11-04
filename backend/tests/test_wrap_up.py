"""调查预算内收尾：隔离模型，真实查询、发现和报告验证。"""

import asyncio
import json
import threading

import test_agent
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.data.models import QueryExecution
from sqlalchemy import func, select
from test_agent import FakeLLM, calls, final_from_query
from test_investigations import enqueue, finding_call, investigation
from test_reports import SQL, report_candidate

agent_workspace = test_agent.agent_workspace


class ClosingModel(FakeLLM):
    """
    作用：记录每轮真实开放的工具签名，仍走统一模型解析路径
    输入：测试回合
    输出：仅测试使用的模型
    """

    def __init__(self, steps):
        """
        作用：初始化回合和工具签名记录
        输入：回合列表
        输出：无
        """
        super().__init__(steps)
        self.toolsets = []

    async def next_turn(self, messages, tools):
        """
        作用：记录实际工具范围并核对配对消息
        输入：上下文与签名
        输出：统一测试回合
        """
        self.toolsets.append({item["function"]["name"] for item in tools})
        return await super().next_turn(messages, tools)


def execute(workspace, steps):
    """
    作用：在正式预算与真实服务上执行收尾场景
    输入：隔离工作区和模型回合
    输出：运行、模型及事件
    """
    config, store, _, _ = workspace
    created = enqueue(workspace, investigation(workspace))
    model = ClosingModel(steps)
    asyncio.run(AgentRuntime(store, config, model).run(store.claim(), threading.Event()))
    return store.read(created["id"]), model, store.events_after(created["id"], 0)


def repaired_finding(messages):
    """
    作用：模拟发现提交参数需要一次修正
    输入：真实上下文
    输出：不符合契约的测试调用
    """
    result = finding_call(messages)
    result[0][1]["unexpected"] = True
    return result


def read_existing(messages):
    """
    作用：只读取实际产生的冻结结果
    输入：真实上下文
    输出：读取调用
    """
    ident = final_from_query(messages)["artifact_ids"][0]
    return calls(("read_artifact", {"artifact_id": ident}))


def invalid_report(messages):
    """
    作用：模拟报告引用错误，要求留出修正轮数
    输入：真实上下文
    输出：待修正候选
    """
    result = report_candidate(messages)
    result["report"]["blocks"][0]["value_ref"]["column"] = "missing_column"
    return result


def repair_saved_candidate(messages):
    """
    作用：确认修复轮能看到原候选，修正引用时无需重新编写已核对文字。
    输入：含服务端错误及上一候选的真实上下文。
    输出：仅修正无效引用列后的候选。
    """
    previous = next(
        json.loads(m["content"])
        for m in reversed(messages)
        if m["role"] == "assistant" and (m.get("content") or "").startswith('{"kind":"report"')
    )
    assert previous["report"]["blocks"][0]["value_ref"]["column"] == "missing_column"
    previous["report"]["blocks"][0]["value_ref"]["column"] = "record_count"
    previous["report"]["blocks"][2]["values"]["count"]["column"] = "record_count"
    return previous


def test_reserves_prepare_and_final_repair_within_sixteen_turns(agent_workspace):
    """
    作用：探索、发现修正及最终报告修正都在既定轮数内完成
    输入：先用完探索阶段的隔离序列
    输出：无
    """
    steps = [calls(("query_sql", {"sql": SQL}))] + [calls(("list_tables", {}))] * 10
    steps += [repaired_finding, finding_call, read_existing, invalid_report, repair_saved_candidate]
    run, model, events = execute(agent_workspace, steps)
    assert run["status"] == "completed" and run["result"]["kind"] == "report", (
        run.get("error_code"),
        run.get("error_details"),
        model.index,
        model.messages[-1][-2:],
    )
    assert model.index == 16 and run["budget"]["max_turns"] == 16
    assert model.toolsets[10] >= {"query_sql", "search_knowledge"}
    assert model.toolsets[11] == {"read_artifact", "record_finding"}
    assert model.toolsets[14] == model.toolsets[15] == set()
    assert [e["payload"]["phase"] for e in events if "phase" in e["payload"]] == [
        "prepare",
        "finalize",
        "report_repair",
    ]
    repair = next(e["payload"] for e in events if e["payload"].get("phase") == "report_repair")
    assert repair["error_code"]
    assert events[-1]["type"] == "run.completed"


def test_unadvertised_late_query_is_denied_and_messages_still_pair(agent_workspace):
    """
    作用：模型忽略收尾限制也无法新增查询，且可改为保存发现和报告
    输入：收尾阶段仍提出 SQL 的回合
    输出：无
    """
    steps = [calls(("query_sql", {"sql": SQL}))] + [calls(("list_tables", {}))] * 10
    steps += [calls(("query_sql", {"sql": SQL})), finding_call, report_candidate]
    run, _, events = execute(agent_workspace, steps)
    assert run["status"] == "completed", run
    assert any(
        e["payload"].get("error_code") == "TOOL_NOT_AVAILABLE_DURING_WRAP_UP" for e in events
    )
    with agent_workspace[1].engine.connect() as c:
        assert (
            c.execute(
                select(func.count())
                .select_from(QueryExecution)
                .where(QueryExecution.run_id == run["id"])
            ).scalar_one()
            == 1
        )
    starts = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.started"]
    ends = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.finished"]
    assert starts == ends


def test_tool_budget_reserves_finding_before_running_out(agent_workspace):
    """
    作用：工具额度先紧张时提前收尾，不增加总额度
    输入：同批多个允许调用
    输出：无
    """
    initial = calls(("query_sql", {"sql": SQL}), *[("list_tables", {})] * 19)
    run, model, _ = execute(agent_workspace, [initial, finding_call, report_candidate])
    assert run["status"] == "completed", run
    assert model.toolsets[1] == {"read_artifact", "record_finding"}
    assert run["budget"]["max_tool_calls"] == 24 and model.index == 3


def test_saved_findings_force_finalization_before_last_tool_slots(agent_workspace):
    """
    作用：已保存发现后，最后少量工具额度必须让位于报告，不允许反复保存耗尽额度。
    输入：同批合成查询及两次发现保存。
    输出：预算不增加，后续工具关闭且报告完成。
    """
    initial = calls(("query_sql", {"sql": SQL}), *[("list_tables", {})] * 19)
    run, model, _ = execute(
        agent_workspace, [initial, finding_call, finding_call, report_candidate]
    )
    assert run["status"] == "completed", run
    assert model.toolsets[1] == model.toolsets[2] == {"read_artifact", "record_finding"}
    assert not model.toolsets[3]
    assert run["budget"]["max_tool_calls"] == 24
