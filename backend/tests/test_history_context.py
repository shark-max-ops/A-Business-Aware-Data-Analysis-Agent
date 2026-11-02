"""批量历史概览与单项引用读取使用真实 SQLite、查询产物和运行预算。"""

import asyncio
import copy
import json
import threading
import time

import test_agent
from metric_pilot.agent.context import fit_context, initial_messages
from metric_pilot.agent.contracts import ToolCall
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.contracts import RunContext
from test_agent import FakeLLM, calls
from test_investigations import enqueue, investigation
from test_reports import SQL, service

agent_workspace = test_agent.agent_workspace


def registry_for(workspace, run):
    """
    作用：组合隔离运行的真实工具服务
    输入：测试工作区与运行
    输出：受限工具注册表
    """
    return ToolRegistry(
        run,
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        service(workspace).query,
        workspace[0],
        threading.Event(),
    )


def history(workspace):
    """
    作用：创建十项拥有完整真实引用的历史，重现多次调查后的大页返回
    输入：隔离工作区
    输出：调查、成功基线运行、产物与固定引用
    """
    inv = investigation(workspace)
    run = enqueue(workspace, inv)
    workspace[1].claim()
    registry = registry_for(workspace, run)
    result = registry.invoke(ToolCall(id="query", name="query_sql", arguments={"sql": SQL}))
    assert result.ok
    artifact = result.artifact_ids[0]
    values = {
        f"n{i}": {"artifact_id": artifact, "row": 0, "column": "record_count"} for i in range(24)
    }
    for i in range(10):
        result = registry.invoke(
            ToolCall(
                id=f"finding{i}",
                name="record_finding",
                arguments={
                    "kind": "fact",
                    "check": "检查合成快照的实际冻结计数",
                    "questions": ["复核合成发现"],
                    "statement": {
                        "id": f"f{i}",
                        "type": "text",
                        "text_template": "合成引用核查："
                        + "，".join("{{n" + str(j) + "}}" for j in range(24)),
                        "values": values,
                        "evidence_ids": [artifact],
                    },
                },
            )
        )
        assert result.ok, result
    workspace[1].finish(
        run["id"], "completed", {"kind": "query_result", "artifact_ids": [artifact]}
    )
    return InvestigationStore(workspace[1].engine).read(inv["id"]), run, artifact, values


def test_history_overview_fits_and_single_item_keeps_exact_references(agent_workspace):
    """
    作用：大页历史压缩为可定位概览，单项读取和旧成功证据授权保持完整
    输入：十项真实历史与正式上下文预算
    输出：无
    """
    inv, _, artifact, refs = history(agent_workspace)
    run = enqueue(agent_workspace, inv)
    registry = registry_for(agent_workspace, run)
    call = ToolCall(id="history", name="read_investigation", arguments={"offset": 0, "limit": 10})
    raw = registry.invoke(call).model_dump()
    original = copy.deepcopy(raw)
    group = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call.id, "content": json.dumps(raw, ensure_ascii=False)},
    ]
    available = (
        32000 - 3000 - len(json.dumps(registry.schemas(), ensure_ascii=False).encode()) - 1024
    )
    assert len(json.dumps(raw, ensure_ascii=False).encode()) > available
    messages = fit_context(initial_messages(run, registry, []), [group], registry, available)
    data = json.loads(messages[-1]["content"])["data"]
    assert len(data["findings"]) == 10
    assert [f["detail_offset"] for f in data["findings"]] == list(range(10))
    assert all(
        "value_refs" not in f and f["source_run_status"] == "completed" for f in data["findings"]
    )
    assert raw == original
    detail_call = ToolCall(id="one", name="read_investigation", arguments={"offset": 4, "limit": 1})
    detail = registry.invoke(detail_call).data
    assert detail["findings"][0]["id"] == data["findings"][4]["id"]
    assert detail["findings"][0]["value_refs"] == refs
    assert registry.invoke(
        ToolCall(id="read", name="read_artifact", arguments={"artifact_id": artifact})
    ).ok


def test_runtime_can_continue_from_large_history_without_new_query(agent_workspace):
    """
    作用：真实运行从历史概览到单项引用并保存反证、生成报告，不重置额度
    输入：隔离大历史及测试模型
    输出：无
    """
    inv, _, _, refs = history(agent_workspace)
    run = enqueue(agent_workspace, inv)
    statement = {
        "type": "text",
        "id": "correction",
        "text_template": "重新核对合成计数为 {{n}}。",
        "values": {"n": refs["n0"]},
        "evidence_ids": [refs["n0"]["artifact_id"]],
    }
    model = FakeLLM(
        [
            calls(("read_investigation", {"offset": 0, "limit": 10}), ("list_tables", {})),
            calls(("read_investigation", {"offset": 0, "limit": 1})),
            calls(
                (
                    "record_finding",
                    {
                        "kind": "counterevidence",
                        "check": "重读合成冻结计数",
                        "statement": statement,
                        "supersedes": inv["findings"][-1]["id"],
                    },
                )
            ),
            {
                "kind": "report",
                "report": {
                    "schema_version": "1",
                    "title": "合成反证核查",
                    "scope": "固定合成快照",
                    "metric_revision_ids": [],
                    "blocks": [statement],
                    "limitations": [],
                },
            },
        ]
    )
    asyncio.run(
        AgentRuntime(agent_workspace[1], agent_workspace[0], model).run(
            agent_workspace[1].claim(), threading.Event()
        )
    )
    finished = agent_workspace[1].read(run["id"])
    assert finished["status"] == "completed" and finished["result"]["kind"] == "report", finished
    updated = InvestigationStore(agent_workspace[1].engine).read(inv["id"])
    assert updated["allocated"]["runs"] == 2 and len(updated["findings"]) == 11
