"""6.C合成验收：范围不能被SQL、续查或旧证据绕过；不调用真实模型。"""

import asyncio
import json
import threading
import time
from datetime import date

import pytest
import test_multimonth
from fastapi.testclient import TestClient
from metric_pilot.agent.contracts import CreateInvestigation, CreateRun
from metric_pilot.agent.investigations import InvestigationStore, read_evidence_artifact
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.agent.store import RunStore
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.multimonth import ingest_months
from metric_pilot.data.query import QueryService
from metric_pilot.data.ranges import DateRange, resolve_range
from metric_pilot.knowledge import models as km
from metric_pilot.knowledge.contracts import DocumentMetadata, MetricWrite, SearchRequest
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.main import create_app
from metric_pilot.reports.contracts import ReportRevisionWrite
from metric_pilot.reports.service import ReportService
from pydantic import SecretStr
from test_agent import FakeLLM, calls
from test_investigations import finding_call
from test_knowledge import FakeKnowledgeProvider, add_document, metric_definition
from test_reports import SQL, report_candidate

BUDGET = {
    "max_turns": 12,
    "max_tool_calls": 20,
    "timeout_seconds": 30,
    "context_tokens": 32000,
    "output_tokens": 3000,
}
SELECTED = DateRange(start="2026-01-31", end_exclusive="2026-02-01")
workspace = test_multimonth.workspace


@pytest.fixture
def ranged(workspace):
    """
    作用：建立有两个月且包含一个范围外记录的真实隔离DuckDB
    输入：明确合成的多月工作区
    输出：配置、目录、快照、查询服务与知识快照
    """
    config, inputs, catalog = workspace
    snapshot = ingest_months(config, inputs, ["2026-01", "2026-02"], synthetic=True)
    query = QueryService(catalog, QueryExecutor(timeout_seconds=10, max_rows=100))
    knowledge = KnowledgeStore(catalog.engine).snapshot()
    return config, inputs, catalog, snapshot, query, knowledge


@pytest.mark.parametrize(
    "sql,params",
    [
        ("SELECT count(*) AS n FROM v_flights", {}),
        ("WITH x AS (SELECT * FROM v_flights) SELECT count(*) AS n FROM x", {}),
        ("SELECT count(*) AS n FROM (SELECT * FROM v_flights UNION SELECT * FROM v_flights) x", {}),
        ("SELECT count(*) AS n FROM v_flights a JOIN v_flights b ON a.record_id=b.record_id", {}),
        (
            "SELECT count(*) AS n FROM v_flights WHERE flight_date>=:start OR 1=1",
            {"start": "1900-01-01"},
        ),
        (
            "SELECT count(*) AS n FROM v_flights WHERE flight_date=:outside",
            {"outside": "2026-02-01"},
        ),
    ],
)
def test_scope_enforced_without_model_filter(ranged, sql, params):
    """
    作用：CTE、集合、连接及恒真条件均只能读取服务端范围内的航班
    输入：真实隔离数据、SQL和模型参数
    输出：无
    """
    _, _, _, snapshot, query, _ = ranged
    ctx = RunContext("manual", snapshot.id, "k", time.monotonic() + 20, SELECTED)
    result = query.execute(ctx, sql, params)
    assert result.ok, result
    page = query.read_artifact("manual", result.artifact_ids[0])
    assert page["rows"] == [[0 if "outside" in params else 1]]
    assert page["artifact"]["date_range"] == SELECTED.model_dump()
    assert '"analytics"."main"."v_flights"' in page["artifact"]["scope_sql"]
    assert page["artifact"]["parameters"] == params


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM analytics.main.v_flights",
        "SELECT * FROM main.v_flights",
        "SELECT * FROM raw_flights",
        "CREATE TEMP VIEW v_flights AS SELECT 1",
    ],
)
def test_physical_table_and_scope_override_rejected(ranged, sql):
    """
    作用：用户不能限定主库、读原始表或覆盖临时视图
    输入：隔离数据及越权SQL
    输出：无，失败不产生证据
    """
    _, _, _, snapshot, query, _ = ranged
    result = query.execute(
        RunContext("manual", snapshot.id, "k", time.monotonic() + 20, SELECTED), sql, {}
    )
    assert not result.ok
    assert not result.artifact_ids


@pytest.mark.parametrize(
    "value",
    [
        {"start": "2026-01-31", "end_exclusive": "2026-01-31"},
        {"start": "2026-02-30", "end_exclusive": "2026-03-02"},
        {"start": "2026-01-01' OR 1=1 --", "end_exclusive": "2026-03-01"},
    ],
)
def test_date_contract_rejects_invalid_ranges(value):
    """
    作用：日期校验不能接受空范围、非法日历或SQL注入
    输入：非法日期对象
    输出：无
    """
    with pytest.raises(ValueError):
        DateRange.model_validate(value)


def create_investigation(ranged, selected=SELECTED):
    """
    作用：通过真实仓储固定合成调查范围
    输入：隔离环境与日期范围
    输出：调查、运行仓储与调查仓储
    """
    _, _, catalog, snapshot, _, knowledge = ranged
    runs = RunStore(catalog.engine)
    invs = InvestigationStore(catalog.engine)
    conv = runs.conversation()
    inv = invs.create(
        CreateInvestigation(
            conversation_id=conv["id"],
            goal="合成范围核查",
            scope="仅合成数据",
            snapshot_id=snapshot.id,
            date_range=selected,
        ),
        knowledge["id"],
        BUDGET,
    )
    return inv, runs, invs


def enqueue(inv, runs, **changes):
    """
    作用：续查省略范围以检查服务端继承
    输入：固定调查、运行仓储和请求覆盖
    输出：排队运行
    """
    request = {
        "conversation_id": inv["conversation_id"],
        "snapshot_id": inv["snapshot_id"],
        "question": "核查范围",
        "investigation_id": inv["id"],
        "expected_investigation_version": inv["version"],
        **changes,
    }
    return runs.create(CreateRun(**request), inv["knowledge_snapshot_id"], BUDGET)


def test_creation_continuation_and_context_range_boundaries(ranged):
    """
    作用：范围须显式选择、续查继承且不可改动，上下文不能跨范围
    输入：隔离环境
    输出：无
    """
    _, _, catalog, snapshot, _, knowledge = ranged
    runs = RunStore(catalog.engine)
    conv = runs.conversation()
    with pytest.raises(DataError, match="DATE_RANGE_REQUIRED"):
        runs.create(
            CreateRun(conversation_id=conv["id"], snapshot_id=snapshot.id, question="合成"),
            knowledge["id"],
            BUDGET,
        )
    with pytest.raises(DataError, match="DATE_RANGE_OUTSIDE_SNAPSHOT"):
        resolve_range(
            catalog.engine, snapshot.id, DateRange(start="2025-12-31", end_exclusive="2026-02-01")
        )
    inv, runs, invs = create_investigation(ranged)
    first = enqueue(inv, runs)
    assert first["date_range"] == SELECTED.model_dump()
    runs.finish(
        first["id"],
        "completed",
        result={"kind": "clarification", "question": "合成", "missing_requirements": ["合成条件"]},
    )
    inv = invs.read(inv["id"])
    changed = DateRange(start="2026-02-01", end_exclusive="2026-03-01")
    with pytest.raises(DataError, match="INVESTIGATION_BOUNDARY_MISMATCH"):
        enqueue(inv, runs, date_range=changed)
    follow = enqueue(inv, runs)
    assert follow["date_range"] == first["date_range"]
    with pytest.raises(DataError, match="CONTEXT_DATE_RANGE_MISMATCH"):
        runs.create(
            CreateRun(
                conversation_id=inv["conversation_id"],
                snapshot_id=snapshot.id,
                question="新合成范围",
                date_range=changed,
                context_run_ids=[first["id"]],
            ),
            knowledge["id"],
            BUDGET,
        )


def test_artifact_and_executor_cannot_change_frozen_run_scope(ranged):
    """
    作用：查询服务和报告证据均复核范围，不只依赖模型上下文
    输入：隔离环境
    输出：无
    """
    _, _, _, snapshot, query, _ = ranged
    inv, runs, _ = create_investigation(ranged)
    run = enqueue(inv, runs)
    wrong = RunContext(run["id"], snapshot.id, run["knowledge_snapshot_id"], time.monotonic() + 20)
    denied = query.execute(wrong, "SELECT count(*) AS n FROM v_flights", {})
    assert denied.error_code == "RUN_DATE_RANGE_MISMATCH"
    ctx = RunContext(
        run["id"], snapshot.id, run["knowledge_snapshot_id"], time.monotonic() + 20, SELECTED
    )
    result = query.execute(ctx, "SELECT count(*) AS n FROM v_flights", {})
    assert result.ok
    changed = {**run, "date_range": {"start": "2026-02-01", "end_exclusive": "2026-03-01"}}
    with pytest.raises(
        DataError, match="INVESTIGATION_BOUNDARY_MISMATCH|ARTIFACT_DATE_RANGE_MISMATCH"
    ):
        read_evidence_artifact(query, changed, result.artifact_ids[0])
    assert read_evidence_artifact(query, run, result.artifact_ids[0])["rows"] == [[1]]


def test_runtime_report_scope_revision_and_schema_samples(ranged):
    """
    作用：真实执行循环冻结范围、值引用及报告修订，窄范围不暴露全库样本
    输入：合成数据和隔离FakeLLM
    输出：无
    """
    config, _, catalog, snapshot, query, _ = ranged
    inv, runs, _ = create_investigation(ranged)
    enqueue(inv, runs)
    run = runs.claim()
    registry = ToolRegistry(
        run,
        RunContext(
            run["id"], snapshot.id, run["knowledge_snapshot_id"], time.monotonic() + 20, SELECTED
        ),
        query,
        config,
        threading.Event(),
    )
    assert all(not table.sample for table in registry.schema.tables)
    exploration = {
        "population": "固定日期内的合成航班",
        "calculation": "逐日计数及取消比例",
        "missing_values": "不填零",
    }
    fake = FakeLLM(
        [
            calls(
                (
                    "query_sql",
                    {"sql": SQL, "purpose": "exploratory", "exploration_scope": exploration},
                )
            ),
            finding_call,
            report_candidate,
        ]
    )
    asyncio.run(AgentRuntime(runs, config, fake).run(run, threading.Event()))
    completed = runs.read(run["id"])
    assert completed["status"] == "completed", completed
    service = ReportService(query)
    ids = completed["result"]
    report = service.read(ids["report_id"], ids["revision_id"])
    assert report.date_range == SELECTED
    evidence = service.evidence(report.report_id, report.revision_id, "count")
    assert evidence.date_range == SELECTED
    assert evidence.value.raw == 1
    before, hash_before = service.frozen(report.report_id, report.revision_id)
    revised = service.revise(
        report.report_id, ReportRevisionWrite(expected_version=1, report=report.draft)
    )
    assert revised.date_range == SELECTED
    assert service.frozen(report.report_id, report.revision_id) == (before, hash_before)
    # 用户提示中包含实际范围，不把第一个月当作整个多月范围。
    prompt = json.loads(fake.messages[0][1]["content"])
    assert prompt["date_range"] == SELECTED.model_dump()


def metric_snapshot(store, intervals, source_metadata=None):
    """
    作用：创建明确合成的审核修订集合，以构造正常审核界面不产生的冲突边界
    输入：知识仓储、日期区间列表和可选来源元数据
    输出：固定知识快照、指标服务与修订ID
    """
    receipt = add_document(
        store, metadata=source_metadata or DocumentMetadata(source_kind="project_policy")
    )
    service = MetricService(store)
    definition = metric_definition(receipt["revision_id"])
    draft = service.save(MetricWrite(definition=definition, expected_version=0))
    # 仅测试直接插入冲突修订；生产入口仍需正常人工审核。
    ids = []
    with store.write() as c:
        base = store.get(c, km.metric_revisions, draft["id"])
        for index, (start, end) in enumerate(intervals):
            ident = f"synthetic_metric_{index}"
            c.execute(
                km.metric_revisions.insert().values(
                    **{
                        **base,
                        "id": ident,
                        "version": index + 2,
                        "definition": {
                            **definition,
                            "version": index + 2,
                            "review_state": "approved",
                            "effective_from": start,
                            "effective_to": end,
                        },
                    }
                )
            )
            ids.append(ident)
        snapshot = {
            "document_revision_ids": [receipt["revision_id"]],
            "wiki_revision_ids": [],
            "metric_revision_ids": ids,
        }
        c.execute(
            km.snapshots.insert().values(
                id="synthetic_ks", payload=snapshot, created_at="synthetic"
            )
        )
    return "synthetic_ks", service, ids


@pytest.mark.parametrize(
    "intervals,error",
    [
        ([("2026-01-01", "2026-02-01")], "METRIC_NOT_APPLICABLE"),
        ([("2026-01-01", "2026-01-10"), ("2026-01-20", "2026-02-01")], "METRIC_NOT_APPLICABLE"),
        ([("2026-01-01", "2026-02-01"), ("2026-01-10", "2026-01-20")], "METRIC_CONFLICT"),
        (
            [("2026-01-01", "2026-01-15"), ("2026-01-15", "2026-02-01")],
            "METRIC_VERSION_CHANGED_IN_RANGE",
        ),
    ],
)
def test_metric_checks_all_effective_boundaries(ranged, intervals, error):
    """
    作用：首尾适用不代表中间有效，拒绝空档、重叠及静默跨版本
    输入：隔离环境、合成修订区间与错误
    输出：无
    """
    _, _, catalog, _, _, _ = ranged
    ks, service, _ = metric_snapshot(KnowledgeStore(catalog.engine), intervals)
    selected = DateRange(
        start="2026-01-01", end_exclusive="2026-03-01" if len(intervals) == 1 else "2026-02-01"
    )
    with pytest.raises(DataError, match=error):
        service.resolve_range(ks, "arrival_delay_rate", selected)


def test_metric_source_expiry_and_no_automatic_approval(ranged):
    """
    作用：即使指标自称适用，已知来源过期仍须拒绝，不自动延长或批准
    输入：隔离环境
    输出：无
    """
    _, _, catalog, _, _, _ = ranged
    store = KnowledgeStore(catalog.engine)
    ks, service, _ = metric_snapshot(
        store,
        [("2026-01-01", None)],
        DocumentMetadata(source_kind="project_policy", effective_to=date(2026, 2, 1)),
    )
    with pytest.raises(DataError, match="METRIC_SOURCE_NOT_APPLICABLE"):
        service.resolve_range(
            ks, "arrival_delay_rate", DateRange(start="2026-01-01", end_exclusive="2026-03-01")
        )
    row = service.resolve_range(ks, "arrival_delay_rate", SELECTED)
    assert row["definition"]["effective_to"] is None


def test_range_knowledge_search_includes_later_applicable_sources(ranged):
    """
    作用：跨月检索保留后半段才生效的资料并提示不能当作全范围规则
    输入：隔离环境
    输出：无
    """
    config, _, catalog, _, _, _ = ranged
    store = KnowledgeStore(catalog.engine)
    add_document(
        store,
        "ArrDelay synthetic February rule",
        metadata=DocumentMetadata(
            source_kind="project_policy",
            effective_from=date(2026, 2, 1),
            effective_to=date(2026, 3, 1),
        ),
    )
    ks = store.snapshot()
    result = Retriever(store, FakeKnowledgeProvider(config)).search(
        SearchRequest(
            query="ArrDelay",
            snapshot_id=ks["id"],
            as_of=date(2026, 1, 1),
            end_exclusive=date(2026, 3, 1),
        )
    )
    assert result["items"]
    assert any("一部分" in warning for warning in result["warnings"])


def test_api_requires_explicit_range_and_inherits_on_followup(ranged, monkeypatch):
    """
    作用：API多月创建、续查继承与显式分支均保留真实范围，不调用远程模型
    输入：合成环境与隔离模型替身
    输出：无
    """
    config, _, _, snapshot, _, _ = ranged
    config = config.model_copy(
        update={
            "llm_base_url": "http://127.0.0.1:1/v1",
            "llm_api_key": SecretStr("fake"),
            "llm_model": "synthetic-model",
        }
    )
    monkeypatch.setattr(
        "metric_pilot.agent.runtime.AgentProvider",
        lambda _: FakeLLM(
            [
                {
                    "kind": "clarification",
                    "question": "合成澄清",
                    "missing_requirements": ["合成条件"],
                }
            ]
        ),
    )
    with TestClient(create_app(config)) as client:
        conv = client.post("/api/v1/conversations").json()
        body = {
            "conversation_id": conv["id"],
            "snapshot_id": snapshot.id,
            "goal": "合成跨月核查",
            "scope": "合成范围",
        }
        response = client.post("/api/v1/investigations", json=body)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DATE_RANGE_REQUIRED"
        response = client.post(
            "/api/v1/investigations", json={**body, "date_range": SELECTED.model_dump()}
        )
        assert response.status_code == 201, response.text
        inv = response.json()
        request = {
            "conversation_id": conv["id"],
            "snapshot_id": snapshot.id,
            "question": "合成续查",
            "investigation_id": inv["id"],
            "expected_investigation_version": inv["version"],
        }
        denied = client.post(
            "/api/v1/runs",
            json={**request, "date_range": {"start": "2026-02-01", "end_exclusive": "2026-03-01"}},
        )
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "INVESTIGATION_BOUNDARY_MISMATCH"
        response = client.post("/api/v1/runs", json=request)
        assert response.status_code == 202, response.text
        assert response.json()["date_range"] == SELECTED.model_dump()
        branch = client.post(
            "/api/v1/investigations",
            json={
                **body,
                "parent_id": inv["id"],
                "date_range": {"start": "2026-02-01", "end_exclusive": "2026-03-01"},
            },
        )
        assert branch.status_code == 201, branch.text
        assert branch.json()["date_range"] != inv["date_range"]
