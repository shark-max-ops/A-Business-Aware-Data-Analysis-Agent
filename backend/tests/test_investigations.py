"""持续调查使用隔离 FakeLLM；实际执行 SQLite、DuckDB、证据取值与报告保存。"""

import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_agent
from fastapi.testclient import TestClient
from metric_pilot.agent import models as m
from metric_pilot.agent.context import fit_context, initial_messages
from metric_pilot.agent.contracts import CreateInvestigation, CreateRun, ToolCall
from metric_pilot.agent.investigations import (
    InvestigationStore,
    authorized_runs,
    read_evidence_artifact,
)
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import RunContext
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.main import create_app
from sqlalchemy import func, select
from test_agent import FakeLLM, calls, final_from_query
from test_reports import SQL, report_candidate, service

agent_workspace = test_agent.agent_workspace
BUDGET = {
    "max_turns": 16,
    "max_tool_calls": 24,
    "timeout_seconds": 30,
    "context_tokens": 32000,
    "output_tokens": 3000,
}


def investigation(workspace, **overrides):
    """
    作用：建立隔离调查与不可变版本
    输入：工作区和边界覆盖
    输出：调查
    """
    _, store, snapshot, _ = workspace
    conv = store.conversation()
    body = CreateInvestigation(
        conversation_id=conv["id"],
        snapshot_id=snapshot.id,
        goal="调查记录分布",
        scope="仅合成月份，不推断原因",
        **overrides,
    )
    return InvestigationStore(store.engine).create(
        body, KnowledgeStore(store.engine).snapshot()["id"], BUDGET
    )


def enqueue(workspace, inv, **overrides):
    """
    作用：按当前版本提交调查运行
    输入：隔离工作区、调查及请求覆盖
    输出：排队运行
    """
    _, store, _, _ = workspace
    args = {
        "conversation_id": inv["conversation_id"],
        "snapshot_id": inv["snapshot_id"],
        "question": "继续核查实际记录",
        "investigation_id": inv["id"],
        "expected_investigation_version": inv["version"],
        **overrides,
    }
    return store.create(CreateRun(**args), inv["knowledge_snapshot_id"], BUDGET)


def run_steps(workspace, inv, steps):
    """
    作用：通过真实 Runtime 执行隔离模型回合
    输入：工作区、调查与回合
    输出：运行和模型
    """
    config, store, _, _ = workspace
    run = enqueue(workspace, inv)
    claimed = store.claim()
    assert claimed["id"] == run["id"]
    model = FakeLLM(steps)
    asyncio.run(AgentRuntime(store, config, model).run(claimed, threading.Event()))
    return store.read(run["id"]), model


def finding_call(messages):
    """
    作用：从实际查询结果构造引用，不预填查询数字
    输入：工具消息
    输出：发现工具调用
    """
    ident = final_from_query(messages)["artifact_ids"][0]
    return calls(
        (
            "record_finding",
            {
                "kind": "fact",
                "check": "已按日期检查记录分布",
                "statement": {
                    "type": "text",
                    "id": "observed",
                    "text_template": "首日记录为 {{n}} 条。",
                    "values": {"n": {"artifact_id": ident, "row": 0, "column": "record_count"}},
                    "evidence_ids": [ident],
                },
                "questions": ["检查其他日期是否存在不同分布"],
            },
        )
    )


def first_report(workspace, inv):
    """
    作用：完成实际查询、发现与报告
    输入：工作区与调查
    输出：完成运行
    """
    run, _ = run_steps(
        workspace, inv, [calls(("query_sql", {"sql": SQL})), finding_call, report_candidate]
    )
    assert run["status"] == "completed", run
    return run


def test_continue_reuses_authorized_evidence_and_preserves_history(agent_workspace):
    """
    作用：验证继续调查、跨运行取值、反证修订和旧报告不变
    输入：隔离工作区
    输出：无
    """
    inv = investigation(agent_workspace)
    first = first_report(agent_workspace, inv)
    repo = InvestigationStore(agent_workspace[1].engine)
    inv = repo.read(inv["id"])
    old = inv["findings"][0]
    evidence_before = repo.evidence(inv["id"], old["id"])
    reports = service(agent_workspace)
    old_report = reports.frozen(first["result"]["report_id"], first["result"]["revision_id"])
    ident = old["artifact_ids"][0]
    statement = {
        "type": "text",
        "id": "counter",
        "text_template": "原查询首日仍为 {{n}} 条，但不能据此判断原因。",
        "values": old["value_refs"],
        "evidence_ids": [ident],
    }
    statement["values"] = {"n": next(iter(old["value_refs"].values()))}
    candidate = {
        "kind": "report",
        "report": {
            "schema_version": "1",
            "title": "进一步核查",
            "scope": inv["scope"],
            "metric_revision_ids": [],
            "blocks": [statement],
            "limitations": ["缺乏因果证据"],
        },
    }
    second, model = run_steps(
        agent_workspace,
        inv,
        [
            calls(("read_investigation", {})),
            calls(("read_artifact", {"artifact_id": ident})),
            calls(
                (
                    "record_finding",
                    {
                        "kind": "counterevidence",
                        "check": "重读冻结结果并核对解释边界",
                        "statement": statement,
                        "supersedes": old["id"],
                        "questions": ["是否有其他解释证据"],
                    },
                )
            ),
            candidate,
        ],
    )
    assert second["status"] == "completed", second
    current = repo.read(inv["id"])
    assert len(current["findings"]) == 2
    assert current["findings"][-1]["supersedes"] == old["id"]
    assert repo.evidence(inv["id"], old["id"]) == evidence_before
    assert (
        reports.frozen(first["result"]["report_id"], first["result"]["revision_id"]) == old_report
    )
    new_report = reports.frozen(second["result"]["report_id"], second["result"]["revision_id"])[0]
    assert new_report["artifacts"][ident]["artifact"]["run_id"] == first["id"]
    assert inv["knowledge_snapshot_id"] in json.dumps(model.messages, ensure_ascii=False)
    assert current["allocated"]["runs"] == 2


def test_followup_query_depends_on_real_tool_result(agent_workspace):
    """
    作用：验证实际结果驱动下一步而非生产硬编码问题路线
    输入：隔离数据
    输出：无
    """
    inv = investigation(agent_workspace)

    def choose_query(messages):
        """
        作用：测试中依据真实 COUNT 分支核查
        输入：模型收到的工具结果
        输出：下一次查询
        """
        actual = json.loads(messages[-1]["content"])["data"]["preview"][0][0]
        assert isinstance(actual, int)
        return calls(
            (
                "query_sql",
                {"sql": SQL if actual > 0 else "SELECT COUNT(*) AS record_count FROM v_flights"},
            )
        )

    run, model = run_steps(
        agent_workspace,
        inv,
        [
            calls(("query_sql", {"sql": "SELECT COUNT(*) FROM v_flights"})),
            choose_query,
            finding_call,
            report_candidate,
        ],
    )
    assert run["status"] == "completed"
    assert len(model.messages) == 4
    assert any(
        e["type"] == "tool.finished" and e["payload"].get("tool") == "record_finding"
        for e in agent_workspace[1].events_after(run["id"], 0)
    )


def test_no_finding_of_duplicates_is_a_valid_report(agent_workspace):
    """
    作用：没有检测到指定问题时如实生成报告，不强行制造异常
    输入：隔离快照与真实唯一标识检查
    输出：无
    """
    inv = investigation(agent_workspace)

    def statement(messages):
        """
        作用：引用真实重复计数，保留检测范围
        输入：实际工具消息
        输出：受限说明块
        """
        ident = final_from_query(messages)["artifact_ids"][0]
        return {
            "type": "text",
            "id": "quality",
            "text_template": "重复记录标识的计数为 {{n}}，本项检查没有发现重复。",
            "values": {"n": {"artifact_id": ident, "row": 0, "column": "duplicate_ids"}},
            "evidence_ids": [ident],
        }

    def record(messages):
        """
        作用：保存实际检查的无异常观察
        输入：实际工具消息
        输出：记录发现调用
        """
        return calls(
            (
                "record_finding",
                {
                    "kind": "fact",
                    "check": "比较总记录数与去重记录标识数",
                    "statement": statement(messages),
                },
            )
        )

    def report(messages):
        """
        作用：结束限定范围的无异常检查
        输入：实际工具消息
        输出：报告候选
        """
        return {
            "kind": "report",
            "report": {
                "schema_version": "1",
                "title": "记录标识检查",
                "scope": "固定合成快照，仅检查记录标识重复",
                "metric_revision_ids": [],
                "blocks": [statement(messages)],
                "limitations": ["不能据此宣称所有数据质量问题均已排除"],
            },
        }

    run, _ = run_steps(
        agent_workspace,
        inv,
        [
            calls(
                (
                    "query_sql",
                    {
                        "sql": "SELECT COUNT(*) - COUNT(DISTINCT record_id) "
                        "AS duplicate_ids FROM v_flights"
                    },
                )
            ),
            record,
            report,
        ],
    )
    assert run["status"] == "completed", run
    frozen, _ = service(agent_workspace).frozen(
        run["result"]["report_id"], run["result"]["revision_id"]
    )
    assert frozen["blocks"][0]["segments"][1]["value"]["raw"] == 0


def test_latest_structure_batch_survives_real_context_budget(agent_workspace):
    """
    作用：真实表结构与调查工具同批返回时不会被裁剪成重复准备循环
    输入：隔离工作区及正式上下文契约
    输出：无
    """
    config, store, _, _ = agent_workspace
    inv = investigation(agent_workspace)
    run = enqueue(agent_workspace, inv)
    registry = ToolRegistry(
        run,
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        service(agent_workspace).query,
        config,
        threading.Event(),
    )
    calls = [
        ToolCall(id="review", name="read_investigation", arguments={}),
        ToolCall(id="tables", name="list_tables", arguments={}),
        ToolCall(id="schema", name="describe_table", arguments={"table_name": "v_flights"}),
    ]
    group = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in calls
            ],
        }
    ]
    group.extend(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(registry.invoke(call).model_dump(), ensure_ascii=False),
        }
        for call in calls
    )
    original = copy.deepcopy(group)
    available = (
        32000 - 3000 - len(json.dumps(registry.schemas(), ensure_ascii=False).encode()) - 1024
    )
    messages = fit_context(initial_messages(run, registry, []), [group], registry, available)
    assert {msg["tool_call_id"] for msg in messages if msg["role"] == "tool"} == {
        "review",
        "tables",
        "schema",
    }
    pinned = next(
        json.loads(message["content"])["known_table_columns"]
        for message in messages
        if message["role"] == "system" and '"known_table_columns"' in message["content"]
    )
    assert pinned["v_flights"]["flight_date"] == "DATE"
    assert group == original


def test_concurrent_continuations_are_serialized(agent_workspace):
    """
    作用：并发续查只接受一个，拒绝请求不消耗额度或留下运行
    输入：隔离工作区
    输出：无
    """
    inv = investigation(agent_workspace)

    def submit(_):
        """
        作用：捕获并发请求的明确结果
        输入：占位索引
        输出：运行或错误码
        """
        try:
            return enqueue(agent_workspace, inv)["id"]
        except DataError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(value.startswith("run_") for value in results) == 1
    assert "INVESTIGATION_BUSY" in results
    current = InvestigationStore(agent_workspace[1].engine).read(inv["id"])
    assert current["allocated"]["runs"] == len(current["run_ids"]) == 1


def test_budget_cancel_restart_and_stale_version(agent_workspace):
    """
    作用：取消和重启不重置总额度，过期版本不能写入
    输入：隔离工作区
    输出：无
    """
    inv = investigation(agent_workspace)
    store = agent_workspace[1]
    repo = InvestigationStore(store.engine)
    first = enqueue(agent_workspace, inv)
    store.cancel(first["id"])
    with pytest.raises(DataError, match="VERSION_CONFLICT"):
        enqueue(agent_workspace, inv)
    for index in range(3):
        current = repo.read(inv["id"])
        run = enqueue(agent_workspace, current)
        if index == 0:
            store.claim()
            store.recover()
            assert store.read(run["id"])["status"] == "interrupted"
        else:
            store.cancel(run["id"])
    current = repo.read(inv["id"])
    assert current["status"] == "budget_exhausted"
    with pytest.raises(DataError, match="BUDGET_EXCEEDED"):
        enqueue(agent_workspace, current)
    assert repo.read(inv["id"])["allocated"] == current["allocated"]


@pytest.mark.parametrize(
    "case", ["other_investigation", "knowledge", "data", "failed_owner", "unlinked"]
)
def test_cross_run_evidence_requires_boundary_and_completed_owner(agent_workspace, case):
    """
    作用：拒绝跨调查、错版本、未关联或失败运行的产物
    输入：隔离工作区及错误类型
    输出：无
    """
    inv = investigation(agent_workspace)
    first = first_report(agent_workspace, inv)
    repo = InvestigationStore(agent_workspace[1].engine)
    old = repo.read(inv["id"])["findings"][0]
    target = (
        investigation(agent_workspace) if case == "other_investigation" else repo.read(inv["id"])
    )
    run = enqueue(agent_workspace, target)
    if case in {"knowledge", "data"}:
        run = {**run, "knowledge_snapshot_id" if case == "knowledge" else "snapshot_id": "forged"}
    if case == "failed_owner":
        with agent_workspace[1].write() as c:
            c.execute(m.runs.update().where(m.runs.c.id == first["id"]).values(status="failed"))
    if case == "unlinked":
        run = {**run, "id": "run_forged"}
    with pytest.raises(DataError):
        read_evidence_artifact(service(agent_workspace).query, run, old["artifact_ids"][0])


def test_missing_evidence_and_supersedes_are_rejected(agent_workspace):
    """
    作用：拒绝无依据事实与跨调查修正，允许诚实的无发现限制
    输入：隔离工作区
    输出：无
    """
    inv = investigation(agent_workspace)
    plain = {
        "type": "text",
        "id": "limit",
        "text_template": "没有足够证据判断原因。",
        "values": {},
        "evidence_ids": [],
    }
    run, model = run_steps(
        agent_workspace,
        inv,
        [
            calls(("record_finding", {"kind": "fact", "check": "无引用", "statement": plain})),
            calls(
                (
                    "record_finding",
                    {
                        "kind": "limitation",
                        "check": "确认缺少因果证据",
                        "statement": plain,
                        "supersedes": "finding_foreign",
                    },
                )
            ),
            calls(
                (
                    "record_finding",
                    {"kind": "limitation", "check": "确认缺少因果证据", "statement": plain},
                )
            ),
            {
                "kind": "clarification",
                "question": "请补充原因证据",
                "missing_requirements": ["额外资料"],
            },
        ],
    )
    assert run["status"] == "completed", run
    payload = json.dumps(model.messages)
    assert "FINDING_EVIDENCE_REQUIRED" in payload and "FINDING_NOT_FOUND" in payload
    entries = InvestigationStore(agent_workspace[1].engine).read(inv["id"])["findings"]
    assert len(entries) == 1 and entries[0]["kind"] == "limitation"


def test_finding_checkpoint_survives_failed_run_but_artifact_cannot_be_reused(agent_workspace):
    """
    作用：保留失败前核查记录但拒绝将失败运行当报告证据来源
    输入：隔离工作区
    输出：无
    """
    inv = investigation(agent_workspace)
    run, _ = run_steps(
        agent_workspace,
        inv,
        [calls(("query_sql", {"sql": SQL})), finding_call, calls(("unknown_tool", {}))],
    )
    assert run["status"] == "failed"
    repo = InvestigationStore(agent_workspace[1].engine)
    current = repo.read(inv["id"])
    assert len(current["findings"]) == 1
    assert repo.evidence(inv["id"], current["findings"][0]["id"])["artifacts"]
    new = enqueue(agent_workspace, current)
    assert run["id"] not in authorized_runs(repo.engine, new)


def test_investigation_api_preserves_knowledge_and_explicit_branch(agent_workspace, monkeypatch):
    """
    作用：验证真实 API、固定知识版本和显式分支，未批准实际规则
    输入：隔离工作区和模型替换
    输出：无
    """
    config, store, snapshot, _ = agent_workspace
    monkeypatch.setattr(
        "metric_pilot.agent.runtime.AgentProvider",
        lambda _: FakeLLM(
            [
                {
                    "kind": "clarification",
                    "question": "请确认比较范围",
                    "missing_requirements": ["范围"],
                }
            ]
        ),
    )
    with TestClient(create_app(config)) as client:
        conv = client.post("/api/v1/conversations").json()
        body = {
            "conversation_id": conv["id"],
            "snapshot_id": snapshot.id,
            "goal": "比较记录",
            "scope": "合成月份",
        }
        response = client.post("/api/v1/investigations", json=body)
        assert response.status_code == 201, response.text
        inv = response.json()
        run = client.post(
            "/api/v1/runs",
            json={
                "conversation_id": conv["id"],
                "snapshot_id": snapshot.id,
                "question": "继续核查",
                "investigation_id": inv["id"],
                "expected_investigation_version": inv["version"],
            },
        )
        assert run.status_code == 202, run.text
        assert run.json()["knowledge_snapshot_id"] == inv["knowledge_snapshot_id"]
        branch = client.post(
            "/api/v1/investigations",
            json={**body, "parent_id": inv["id"], "scope": "明确改变比较范围"},
        )
        assert branch.status_code == 201
        assert branch.json()["parent_id"] == inv["id"] and branch.json()["allocated"]["runs"] == 0
        assert (
            len(client.get(f"/api/v1/investigations?conversation_id={conv['id']}").json()["items"])
            == 2
        )
        assert (
            client.get(
                f"/api/v1/investigations/{inv['id']}/findings/finding_unknown/evidence"
            ).status_code
            == 404
        )
    with store.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(m.investigations)).scalar_one() == 2
