"""隔离 FakeLLM 验收真实执行路径；正式配置没有测试模型入口。"""

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from metric_pilot.agent.context import fit_context
from metric_pilot.agent.contracts import CreateRun, ToolCall
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.agent.store import RunStore
from metric_pilot.data.common import DataError
from metric_pilot.data.ingest import ingest
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge.contracts import MetricWrite, VersionAction
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.main import create_app
from sqlalchemy import select
from test_foundation import migrate, settings
from test_knowledge import add_document, metric_definition

ROOT = Path(__file__).resolve().parents[2]


def calls(*items):
    """
    作用：构造仅测试使用的原生调用
    输入：工具及参数
    输出：模型回合
    """
    return [(name, args) for name, args in items]


class FakeLLM:
    """
    作用：测试预置回合且核对消息配对
    输入：回合列表
    输出：隔离模型
    """

    def __init__(self, steps):
        """
        作用：保存测试计划
        输入：回合
        输出：无
        """
        self.steps, self.messages, self.index = steps, [], 0

    async def next_turn(self, messages, tools):
        """
        作用：模拟传输边界
        输入：真实上下文
        输出：调用或候选
        """
        self.messages.append(messages)
        pending = set()
        for message in messages:
            if message["role"] == "assistant" and message.get("tool_calls"):
                assert not pending
                pending.update(c["id"] for c in message["tool_calls"])
            elif message["role"] == "tool":
                assert message["tool_call_id"] in pending
                pending.remove(message["tool_call_id"])
            else:
                assert not pending
        assert not pending
        step = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        if callable(step):
            step = step(messages)
        if isinstance(step, list):
            raw_calls = [
                {
                    "id": f"call_{self.index}_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(step)
            ]
            return AgentProvider(None).parse(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": raw_calls,
                                "reasoning_content": "private-thought",
                            },
                        }
                    ]
                },
                1,
            )
        return {"calls": [], "candidate": step, "usage": {}, "attempts": 1}


def final_from_query(messages):
    """
    作用：使用实际工具产物
    输入：上下文
    输出：有效最终候选
    """
    for msg in reversed(messages):
        if msg["role"] == "tool":
            result = json.loads(msg["content"])
            if result.get("artifact_ids"):
                return {"kind": "query_result", "artifact_ids": result["artifact_ids"]}
    raise AssertionError("actual artifact missing")


@pytest.fixture
def agent_workspace(tmp_path, monkeypatch):
    """
    作用：准备独立数据与已审核测试指标
    输入：临时目录
    输出：运行依赖
    """
    migrate(monkeypatch, tmp_path)
    config = settings(
        tmp_path,
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="fake",
        llm_model="test-tool-model",
    )
    snapshot = ingest(
        config, ROOT / "fixtures/bts_reporting_synthetic.csv", "2026-01", synthetic=True
    )
    engine = create_metadata_engine(config)
    store = RunStore(engine)
    knowledge = KnowledgeStore(engine)
    document = add_document(knowledge)
    service = MetricService(knowledge)
    draft = service.save(
        MetricWrite(definition=metric_definition(document["revision_id"]), expected_version=0)
    )
    approved = service.approve(
        "arrival_delay_rate", VersionAction(revision_id=draft["id"], expected_version=1)
    )
    yield config, store, snapshot, approved
    engine.dispose()


def new_run(workspace, **overrides):
    """
    作用：固定新运行
    输入：依赖及预算覆盖
    输出：已领取运行
    """
    config, store, snapshot, _ = workspace
    conv = store.conversation()
    knowledge = KnowledgeStore(store.engine).snapshot()
    budget = {
        "max_turns": 16,
        "max_tool_calls": 24,
        "timeout_seconds": 30,
        "context_tokens": 32000,
        "output_tokens": 3000,
        **overrides,
    }
    created = store.create(
        CreateRun(conversation_id=conv["id"], snapshot_id=snapshot.id, question="合成数据验证"),
        knowledge["id"],
        budget,
    )
    store.claim()
    return store.read(created["id"])


def execute(workspace, steps, **budget):
    """
    作用：运行真实框架和工具
    输入：测试模型步骤
    输出：运行与模型记录
    """
    config, store, _, _ = workspace
    run = new_run(workspace, **budget)
    llm = FakeLLM(steps)
    asyncio.run(AgentRuntime(store, config, llm).run(run, threading.Event()))
    return store.read(run["id"]), llm


def test_metric_schema_sql_repair_and_pairing(agent_workspace):
    """
    作用：核对多工具、参数修复、实际查询和规则绑定
    输入：隔离环境
    输出：无
    """
    approved = agent_workspace[3]
    run, llm = execute(
        agent_workspace,
        [
            calls(("resolve_metric", {"metric_key": "arrival_delay_rate"}), ("list_tables", {})),
            calls(("describe_table", {"table_name": "v_flights", "path": "forbidden"})),
            calls(("describe_table", {"table_name": "v_flights"})),
            calls(
                (
                    "query_sql",
                    {
                        "sql": "SELECT missing_column FROM v_flights",
                        "metric_revision_ids": [approved["id"]],
                    },
                )
            ),
            calls(
                (
                    "query_sql",
                    {
                        "sql": "SELECT COUNT(*) AS count FROM v_flights WHERE 1=0",
                        "metric_revision_ids": [approved["id"]],
                    },
                )
            ),
            final_from_query,
        ],
    )
    assert run["status"] == "completed"
    events = agent_workspace[1].events_after(run["id"], 0)
    done = [e for e in events if e["type"] == "tool.finished"]
    assert len(done) == 6
    assert done[2]["payload"]["error_code"] == "INVALID_ARGUMENT"
    assert done[-2]["payload"]["ok"] is False
    artifact = done[-1]["payload"]["data"]["artifact"]
    assert artifact["metric_revision_ids"] == [approved["id"]]
    assert artifact["knowledge_revision_ids"]
    assert done[-1]["payload"]["data"]["preview"] == [[0]]
    assert len(llm.messages) == 6
    assert "private-thought" not in json.dumps(events)


@pytest.mark.parametrize(
    "steps,budget,expected",
    [
        ([calls(("list_tables", {}))], {"max_turns": 2}, "TURN_BUDGET_EXCEEDED"),
        (
            [calls(("list_tables", {}), ("list_tables", {}))],
            {"max_tool_calls": 1},
            "TOOL_BUDGET_EXCEEDED",
        ),
        ([calls(("unknown_tool", {}))], {}, "REPEATED_TOOL_FAILURE"),
        ([{"kind": "query_result", "artifact_ids": ["qa_forged"]}], {}, "FINAL_CANDIDATE_INVALID"),
    ],
)
def test_budget_and_bounded_failures(agent_workspace, steps, budget, expected):
    """
    作用：核对预算和重复失败终止
    输入：测试边界
    输出：无
    """
    run, _ = execute(agent_workspace, steps, **budget)
    assert run["status"] == "failed"
    assert run["error_code"] == expected


def test_empty_result_is_success(agent_workspace):
    """
    作用：空结果不触发重试
    输入：隔离环境
    输出：无
    """
    run, _ = execute(
        agent_workspace,
        [
            calls(("query_sql", {"sql": "SELECT carrier_id FROM v_flights WHERE 1=0"})),
            final_from_query,
        ],
    )
    assert run["status"] == "completed"
    result = agent_workspace[1].events_after(run["id"], 0)
    artifact = next(
        e["payload"]["data"]["artifact"] for e in result if e["type"] == "tool.finished"
    )
    assert artifact["returned_rows"] == 0


def test_missing_metric_cannot_bypass_with_free_sql(agent_workspace):
    """
    作用：缺审核规则后不能换自由 SQL 绕过
    输入：隔离环境
    输出：无
    """
    run, _ = execute(
        agent_workspace,
        [
            calls(("resolve_metric", {"metric_key": "missing"})),
            calls(("query_sql", {"sql": "SELECT COUNT(*) AS count FROM v_flights"})),
            {
                "kind": "clarification",
                "question": "需要人工审核规则",
                "missing_requirements": ["指标"],
            },
        ],
    )
    events = agent_workspace[1].events_after(run["id"], 0)
    assert any(
        e["payload"].get("error_code") == "METRIC_REVIEW_OR_CLARIFICATION_REQUIRED" for e in events
    )
    assert run["result"]["kind"] == "clarification"
    assert not any(e["payload"].get("artifact_ids") for e in events)


@pytest.mark.parametrize("batched", [True, False])
def test_metric_gate_finishes_clarification_without_repeated_failure(agent_workspace, batched):
    """
    作用：审核阻断后配对所有调用并以澄清结束，不执行查询或生成报告
    输入：单次或同批重复 SQL 的隔离场景
    输出：无
    """
    query = ("query_sql", {"sql": "SELECT COUNT(*) AS count FROM v_flights"})
    steps = [
        calls(("resolve_metric", {"metric_key": "unapproved_metric"})),
        calls(query, query, ("list_tables", {})) if batched else calls(query),
        *([] if batched else [calls(query)]),
    ]
    run, model = execute(agent_workspace, steps)
    assert run["status"] == "completed" and run["error_code"] is None
    assert run["result"]["kind"] == "clarification"
    assert "unapproved_metric" in str(run["result"]["missing_requirements"])
    assert "新知识版本" in run["result"]["question"]
    assert len(model.messages) == (2 if batched else 3)
    events = agent_workspace[1].events_after(run["id"], 0)
    started = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.started"]
    ended = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.finished"]
    assert started == ended
    assert events[-1]["type"] == "run.completed"
    assert not any(e["type"] == "report.ready" or e["payload"].get("artifact_ids") for e in events)
    from metric_pilot.data.models import QueryExecution
    from sqlalchemy import func

    with agent_workspace[1].engine.connect() as c:
        assert (
            c.execute(
                select(func.count())
                .select_from(QueryExecution)
                .where(QueryExecution.run_id == run["id"])
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("cancelled", [False, True])
def test_actual_async_model_request_is_stopped(agent_workspace, cancelled):
    """
    作用：核对总超时和取消关闭实际异步请求
    输入：终止方式
    输出：无
    """

    class SlowModel:
        """
        作用：模拟实际等待中的异步连接
        输入：无
        输出：关闭标记
        """

        closed = False

        async def next_turn(self, messages, tools):
            """
            作用：等待至被取消
            输入：请求
            输出：无
            """
            try:
                # 等实际请求进入等待后再计时取消，避免把提前取消误当作关闭已打开连接。
                if timer:
                    timer.start()
                await asyncio.sleep(10)
            finally:
                self.closed = True

    config, store, _, _ = agent_workspace
    run = new_run(agent_workspace, timeout_seconds=1)
    signal = threading.Event()
    model = SlowModel()
    timer = threading.Timer(0.15, signal.set) if cancelled else None
    started = time.monotonic()
    asyncio.run(AgentRuntime(store, config, model).run(run, signal))
    if timer:
        timer.join()
    assert time.monotonic() - started < 3
    assert model.closed
    assert store.read(run["id"])["error_code"] == ("RUN_CANCELLED" if cancelled else "RUN_TIMEOUT")


def test_restart_closes_tools_and_replays_terminal(agent_workspace):
    """
    作用：核对中断、工具配对、重放与取消幂等
    输入：隔离环境
    输出：无
    """
    _, store, _, _ = agent_workspace
    run = new_run(agent_workspace)
    store.tool_start(run["id"], ToolCall(id="pending", name="query_sql", arguments={}))
    store.recover()
    store.recover()
    assert store.read(run["id"])["status"] == "interrupted"
    events = store.events_after(run["id"], 0)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-2]["type"] == "tool.finished"
    assert events[-1]["type"] == "run.interrupted"
    assert store.events_after(run["id"], events[-1]["seq"]) == []
    assert store.cancel(run["id"])["status"] == "interrupted"


def test_context_trims_whole_groups():
    """
    作用：裁剪不能产生孤立 tool 消息
    输入：完整工具组
    输出：无
    """
    registry = type(
        "Registry",
        (),
        {"artifact_ids": {"qa_preserved"}, "resolved": {"metric_preserved"}, "definitions": {}},
    )()
    group = [
        {"role": "assistant", "tool_calls": [{"id": "one"}]},
        {"role": "tool", "tool_call_id": "one", "content": "x" * 2000},
    ]
    result = fit_context([{"role": "user", "content": "goal"}], [group], registry, 1000)
    assert not any(item["role"] == "tool" for item in result)
    assert "qa_preserved" in json.dumps(result)
    with pytest.raises(DataError, match="CONTEXT_BUDGET_EXCEEDED"):
        fit_context([{"role": "user", "content": "x" * 3000}], [], registry, 1000)


def test_api_sse_replay_and_cross_context(agent_workspace, monkeypatch):
    """
    作用：真实 API 重放终态及拒绝跨会话
    输入：隔离环境
    输出：无
    """
    config, store, snapshot, _ = agent_workspace
    # 停用后台领取以确定性验证排队取消，不替换真实 API 和数据库。
    monkeypatch.setattr(
        "metric_pilot.agent.runtime.AgentWorker.loop", lambda self: self.stop.wait()
    )
    with TestClient(create_app(config)) as client:
        conv = client.post("/api/v1/conversations").json()
        created = client.post(
            "/api/v1/runs",
            json={"conversation_id": conv["id"], "snapshot_id": snapshot.id, "question": "测试"},
        ).json()
        ident = created["id"]
        assert created["status"] == "queued"
        assert client.post(f"/api/v1/runs/{ident}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/v1/runs/{ident}/cancel").json()["seq"] == 2
        stream = client.get(f"/api/v1/runs/{ident}/events", headers={"Last-Event-ID": "1"})
        assert "run.queued" not in stream.text
        assert "run.cancelled" in stream.text
        assert "id: 2" in stream.text
        assert client.get(f"/api/v1/runs/{ident}/events", headers={"Last-Event-ID": "2"}).text == ""
        assert (
            client.get(f"/api/v1/runs/{ident}/events", headers={"Last-Event-ID": "bad"}).status_code
            == 409
        )
        assert client.get("/api/v1/runs/missing/events").status_code == 404
        assert len(client.get(f"/api/v1/runs/{ident}/event-log").json()["items"]) == 2
        other = client.post("/api/v1/conversations").json()
        response = client.post(
            "/api/v1/runs",
            json={
                "conversation_id": other["id"],
                "snapshot_id": snapshot.id,
                "question": "跨会话",
                "context_run_ids": [ident],
            },
        )
        assert response.json()["error"]["code"] == "INVALID_CONTEXT_RUN"
        assert (
            client.post(
                "/api/v1/runs",
                json={
                    "conversation_id": conv["id"],
                    "snapshot_id": snapshot.id,
                    "question": "伪造规则",
                    "metric_revision_ids": ["metric_forged"],
                },
            ).json()["error"]["code"]
            == "METRIC_NOT_IN_SNAPSHOT"
        )


def test_provider_retries_and_discards_reasoning(agent_workspace, monkeypatch):
    """
    作用：传输重试有限且原始思维不落库
    输入：隔离 HTTP
    输出：无
    """
    counter = []
    original = httpx.AsyncClient

    def handler(request):
        """
        作用：模拟 429 后成功
        输入：HTTP 请求
        输出：响应
        """
        counter.append(request)
        if len(counter) < 3:
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "kind": "clarification",
                                    "question": "缺条件",
                                    "missing_requirements": ["日期"],
                                }
                            ),
                            "reasoning_content": "private-thought",
                        },
                    }
                ],
                "usage": {"total_tokens": 9, "reasoning_content": "private-thought"},
            },
        )

    monkeypatch.setattr(
        "metric_pilot.agent.provider.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    turn = asyncio.run(AgentProvider(agent_workspace[0]).next_turn([], []))
    assert len(counter) == 3
    assert turn["attempts"] == 3
    assert turn["usage"] == {"total_tokens": 9}
    assert "private-thought" not in json.dumps(turn)


def test_search_uses_fixed_knowledge_and_rejects_draft_wiki(agent_workspace):
    """
    作用：检索真实片段且拒绝任意 Wiki
    输入：隔离环境
    输出：无
    """
    run, _ = execute(
        agent_workspace,
        [
            calls(("search_knowledge", {"query": "ArrDelay"})),
            calls(("read_wiki_page", {"page_id": "unpublished"})),
            {"kind": "clarification", "question": "需要审核", "missing_requirements": ["规则"]},
        ],
    )
    events = agent_workspace[1].events_after(run["id"], 0)
    finished = [e["payload"] for e in events if e["type"] == "tool.finished"]
    assert finished[0]["ok"]
    assert finished[0]["data"]["mode"] == "text"
    assert finished[0]["data"]["items"][0]["source_kind"] == "project_policy"
    assert finished[1]["error_code"] == "WIKI_NOT_IN_SNAPSHOT"


def test_runtime_cancel_terminates_real_query_child(agent_workspace, monkeypatch):
    """
    作用：端到端取消须回收实际查询子进程
    输入：隔离环境
    输出：无
    """
    import multiprocessing

    from metric_pilot.data.executor import QueryExecutor
    from metric_pilot.data.models import ArtifactRecord
    from sqlalchemy.orm import Session

    config, store, _, _ = agent_workspace
    cancel = threading.Event()
    timers, pids = [], []
    original = QueryExecutor.execute

    def started(pid):
        """
        作用：查询真正开始后取消
        输入：实际 PID
        输出：无
        """
        pids.append(pid)
        timer = threading.Timer(0.15, cancel.set)
        timers.append(timer)
        timer.start()

    def execute_query(self, path, guarded, parameters, deadline, signal, on_started=None):
        """
        作用：添加实际执行观察点
        输入：原始查询参数
        输出：执行结果
        """
        return original(self, path, guarded, parameters, deadline, signal, started)

    monkeypatch.setattr(QueryExecutor, "execute", execute_query)
    tables = ", ".join(f"v_flights t{i}" for i in range(10))
    expression = "+".join(f"COALESCE(t{i}.arrival_delay,0)" for i in range(10))
    model = FakeLLM([calls(("query_sql", {"sql": f"SELECT SUM({expression}) AS x FROM {tables}"}))])
    run = new_run(agent_workspace)
    asyncio.run(AgentRuntime(store, config, model).run(run, cancel))
    for timer in timers:
        timer.join()
    assert pids
    assert not set(pids) & {child.pid for child in multiprocessing.active_children()}
    assert store.read(run["id"])["status"] == "cancelled"
    with Session(store.engine) as session:
        assert not session.scalars(select(ArtifactRecord)).all()
    events = store.events_after(run["id"], 0)
    assert [e["type"] for e in events].count("tool.started") == 1
    assert [e["type"] for e in events].count("tool.finished") == 1


@pytest.mark.parametrize(
    "budget,expected",
    [
        ({"max_turns": 16}, "TURN_BUDGET_EXCEEDED"),
        ({"max_turns": 30, "max_tool_calls": 24}, "TOOL_BUDGET_EXCEEDED"),
    ],
)
def test_default_limits_are_enforced(agent_workspace, budget, expected):
    """
    作用：验证默认十六轮及二十四次调用上限
    输入：预算
    输出：无
    """
    run, _ = execute(agent_workspace, [calls(("list_tables", {}))], **budget)
    assert run["error_code"] == expected


def test_provider_rejects_duplicate_ids_and_drops_thought_arguments():
    """
    作用：验证模型协议与思维元字段不落库
    输入：原生工具回合
    输出：无
    """
    provider = AgentProvider(None)
    raw = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "one",
                            "type": "function",
                            "function": {
                                "name": "list_tables",
                                "arguments": '{"thought":"private-thought"}',
                            },
                        }
                    ]
                },
            }
        ]
    }
    turn = provider.parse(raw, 1)
    assert turn["calls"][0].arguments == {}
    assert "private-thought" not in str(turn)
    raw["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"params":{"x":NaN}}'
    invalid = provider.parse(raw, 1)
    assert invalid["calls"][0].arguments == {"__invalid_json__": True}
    raw["choices"][0]["message"]["tool_calls"] *= 2
    with pytest.raises(DataError, match="MODEL_PROTOCOL_ERROR"):
        provider.parse(raw, 1)


@pytest.mark.parametrize("title", ["航班数据范围", None, "超长" * 20, "标题\n换行", {"bad": True}])
def test_title_metadata_does_not_break_analysis(agent_workspace, title):
    """
    作用：确认可选标题与分析结果分离且无效标题不会破坏分析
    输入：隔离环境和有效或无效标题
    输出：无
    """
    from metric_pilot.agent import models as m

    run, _ = execute(
        agent_workspace,
        [
            {
                "kind": "clarification",
                "question": "需要收入数据",
                "missing_requirements": ["收入"],
                "conversation_title": title,
            }
        ],
    )
    assert run["status"] == "completed"
    assert "conversation_title" not in run["result"]
    with agent_workspace[1].engine.connect() as connection:
        actual = connection.execute(
            select(m.conversations.c.title).where(m.conversations.c.id == run["conversation_id"])
        ).scalar_one()
    assert actual == ("航班数据范围" if title == "航班数据范围" else run["question"])


def test_late_title_cannot_replace_newer_question(agent_workspace, monkeypatch):
    """
    作用：保护新问题标题并保留完整长问题
    输入：隔离数据库
    输出：无
    """
    from metric_pilot.agent import models as m

    monkeypatch.setattr("metric_pilot.agent.store.utc_now", lambda: "2026-01-01T00:00:00+00:00")
    old = new_run(agent_workspace)
    store = agent_workspace[1]
    question = "请核对这份数据的覆盖范围和记录数量，" * 8
    newer = store.create(
        CreateRun(
            conversation_id=old["conversation_id"],
            snapshot_id=old["snapshot_id"],
            question=question,
        ),
        old["knowledge_snapshot_id"],
        old["budget"],
    )
    store.finish(
        old["id"],
        "completed",
        {"kind": "clarification", "question": "缺少数据", "missing_requirements": ["数据"]},
        conversation_title="旧话题",
    )
    with store.write() as connection:
        assert not store.apply_title(
            connection,
            store.require(connection, newer["id"]),
            "新话题",
            expected_title="已经改变的标题",
        )
        actual = connection.execute(
            select(m.conversations.c.title).where(m.conversations.c.id == old["conversation_id"])
        ).scalar_one()
    assert actual == question


@pytest.mark.parametrize("response_title", ["航空公司利润", "错误\n标题"])
def test_existing_title_generation_uses_only_question(agent_workspace, monkeypatch, response_title):
    """
    作用：验证旧标题补全使用真实适配边界但不发送工具结果
    输入：隔离环境和模拟 HTTP 响应
    输出：无
    """
    from metric_pilot.agent.titles import generate_title

    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        """
        作用：检查独立标题请求的结构并返回隔离响应
        输入：HTTP 请求
        输出：模拟模型响应
        """
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps({"conversation_title": response_title}),
                            "reasoning_content": "private",
                        },
                    }
                ],
                "usage": {"total_tokens": 20},
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    if "\n" in response_title:
        with pytest.raises(DataError, match="INVALID_CONVERSATION_TITLE"):
            asyncio.run(generate_title(agent_workspace[0], "计算航空公司利润"))
    else:
        assert asyncio.run(generate_title(agent_workspace[0], "计算航空公司利润")) == (
            "航空公司利润",
            {"total_tokens": 20},
            1,
        )
    assert len(requests) == 1
    assert "tools" not in requests[0]
    assert json.loads(requests[0]["messages"][1]["content"]) == {"question": "计算航空公司利润"}
