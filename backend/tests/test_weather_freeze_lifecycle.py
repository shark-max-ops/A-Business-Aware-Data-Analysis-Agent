"""天气批量写入、取消/超时和生产工具状态；全部使用明确合成来源。"""

import asyncio
import json
import threading
import time
from datetime import date, datetime, timedelta

import duckdb
import pytest
import test_agent
from metric_pilot.agent.contracts import ToolCall
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import RunContext, ToolResult
from metric_pilot.external.weather_snapshot import build_weather_database, weather_rows
from test_agent import new_run
from test_external_evidence import services, weather_capture
from test_investigations import enqueue, investigation
from test_reports import SQL, report_candidate

agent_workspace = test_agent.agent_workspace


@pytest.mark.parametrize("tamper", [False, True])
def test_weather_freeze_validates_conditional_confirmation(agent_workspace, tamper):
    """
    作用：天气冻结同时核对原件与官方版本确认，不接受不匹配的条件请求。
    输入：隔离工作区、明确合成的历史PSV和304响应。
    输出：无。
    """
    from datetime import UTC

    from metric_pilot.data.common import canonical_bytes
    from metric_pilot.external.http import SourceHTTP
    from metric_pilot.external.weather import WeatherRequest, query_historical_weather
    from test_external_cache import URL
    from test_external_weather import Pool, ghcnh_sample

    async def verify():
        """作用：执行实际采集和冻结；输入：无；输出：无。"""
        config, store, _, _ = agent_workspace
        first = SourceHTTP(
            config.storage_dir,
            pool_factory=lambda: Pool((ghcnh_sample(),), headers=[(b"etag", b'"synthetic"')]),
        )
        _, original = await first.get(URL)
        original["fetched_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        (first.root / (original["id"] + ".json")).write_bytes(canonical_bytes(original))
        client = SourceHTTP(
            config.storage_dir, historical_cache=True, pool_factory=lambda: Pool(status=304)
        )
        weather = await query_historical_weather(
            WeatherRequest(airport="ATL", start="2026-01-25"), config.storage_dir, http=client
        )
        assert weather["acquisition"]["revalidations"]
        created = enqueue(agent_workspace, investigation(agent_workspace))
        store.claim()
        service, _, _ = services(agent_workspace)
        if tamper:
            weather["acquisition"]["revalidations"][0]["if_none_match"] = '"different"'
            (config.storage_dir / "external/weather" / (weather["id"] + ".json")).write_bytes(
                canonical_bytes(weather)
            )
            with pytest.raises(DataError, match="EXTERNAL_DERIVATION_MISMATCH"):
                await service.freeze_weather(created["id"], weather["id"])
        else:
            frozen = await service.freeze_weather(created["id"], weather["id"])
            assert [f["http_status"] for f in frozen["fetches"]] == [200, 304]
            assert frozen["fetches"][0]["fetched_at"] == original["fetched_at"]

    asyncio.run(verify())


def test_repeated_history_compaction_preserves_locations_and_refs():
    """
    作用：长调查历史多次精简不会丢失定位、引用标志或已归组引用。
    输入：明确合成的多项历史和单项引用。
    输出：无。
    """
    from metric_pilot.agent.context import compact_group

    finding = dict(
        id="synthetic_f",
        run_id="synthetic_r",
        source_run_status="completed",
        kind="fact",
        check="synthetic",
        text="synthetic observation",
        artifact_ids=["synthetic_a"],
        value_refs={"n": dict(artifact_id="synthetic_a", row=0, column="n")},
        supersedes=None,
    )
    for findings in ([finding, {**finding, "id": "synthetic_f2"}], [finding]):
        messages = [
            dict(
                role="tool",
                tool_call_id="synthetic_call",
                content=json.dumps(dict(ok=True, data=dict(findings=findings, offset=3))),
            )
        ]
        first = compact_group(messages, brief=True)
        second = compact_group(first, brief=True)
        assert json.loads(first[0]["content"]) == json.loads(second[0]["content"])


def test_weather_timeout_does_not_crash_runtime(agent_workspace, monkeypatch):
    """
    作用：外部超时真实回传后仍能进入下一模型轮并正常澄清，不升级为内部错误。
    输入：隔离模型与合成超时。
    输出：无。
    """
    from metric_pilot.agent.external_tools import ExternalTools
    from metric_pilot.agent.runtime import AgentRuntime
    from test_agent import FakeLLM, calls

    async def timed_out(self, name, args):
        """作用：模拟来源超时；输入：工具参数；输出：安全异常。"""
        raise DataError("EXTERNAL_TIMEOUT")

    monkeypatch.setattr(ExternalTools, "execute", timed_out)
    config, store, _, _ = agent_workspace
    created = enqueue(agent_workspace, investigation(agent_workspace))
    run = store.claim()
    model = FakeLLM(
        [
            calls(
                ("read_investigation", {}),
                (
                    "query_historical_weather",
                    dict(airport="ATL", start="2026-01-24", end="2026-01-26", refresh=True),
                ),
            ),
            dict(
                kind="clarification", question="来源暂不可用", missing_requirements=["成功天气观测"]
            ),
        ]
    )
    asyncio.run(AgentRuntime(store, config, model).run(run, threading.Event()))
    assert store.read(created["id"])["status"] == "completed"
    assert len(model.messages) == 2


def test_artifact_pages_accumulate_without_gaps_or_overwriting_complete_cache(agent_workspace):
    """
    作用：连续分页形成完整的小聚合缓存，跳页不伪造完整，后续小页不覆盖已有完整结果。
    输入：真实合成查询及不同读取顺序。
    输出：无。
    """
    config, store, _, _ = agent_workspace
    created = enqueue(agent_workspace, investigation(agent_workspace))
    store.claim()
    run = store.read(created["id"])
    _, query, _ = services(agent_workspace)
    registry = ToolRegistry(
        run,
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        query,
        config,
        threading.Event(),
    )
    result = registry.invoke(ToolCall(id="synthetic_sql", name="query_sql", arguments={"sql": SQL}))
    assert result.ok
    artifact = result.artifact_ids[0]
    wanted = query.read_artifact(run["id"], artifact, 0, 100)["rows"]
    assert len(wanted) > 1
    registry.query_summaries = []

    def read(offset):
        """作用：经正式工具读取单行；输入：原始行偏移；输出：工具结果。"""
        response = registry.invoke(
            ToolCall(
                id=f"synthetic_page_{offset}",
                name="read_artifact",
                arguments=dict(artifact_id=artifact, offset=offset, limit=1),
            )
        )
        assert response.ok
        return response

    read(1)
    assert not registry.query_summaries[-1]["result_preview"]["complete"]
    assert registry.query_summaries[-1]["result_preview"]["rows"] == []
    for offset in range(len(wanted)):
        read(offset)
    assert registry.query_summaries[-1]["result_preview"]["complete"]
    assert registry.query_summaries[-1]["result_preview"]["rows"] == wanted
    smaller = read(0)
    assert len(smaller.data["rows"]) == 1
    assert registry.query_summaries[-1]["result_preview"]["rows"] == wanted


def test_template_failure_explains_unused_refs_and_can_repair(agent_workspace):
    """
    作用：模型能收到确切占位符错误并修复，失败的发现不落库。
    输入：真实合成查询产物与不匹配的模板。
    输出：无。
    """
    config, store, _, _ = agent_workspace
    created = enqueue(agent_workspace, investigation(agent_workspace))
    store.claim()
    run = store.read(created["id"])
    _, query, _ = services(agent_workspace)
    registry = ToolRegistry(
        run,
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        query,
        config,
        threading.Event(),
    )
    result = registry.invoke(ToolCall(id="synthetic_sql", name="query_sql", arguments={"sql": SQL}))
    assert result.ok
    args = dict(
        kind="fact",
        check="合成记录数量核对",
        statement=dict(
            type="text",
            id="synthetic",
            text_template="这是合成统计。",
            values=dict(n=dict(artifact_id=result.artifact_ids[0], row=0, column="record_count")),
        ),
    )
    failed = registry.invoke(ToolCall(id="synthetic_bad", name="record_finding", arguments=args))
    assert failed.error_code == "REPORT_TEMPLATE_KEYS" and failed.retryable
    assert failed.data["error_details"] == {"unused_refs": ["n"], "missing_refs": []}
    args["statement"]["text_template"] = "首日合成记录 {{n}} 条。"
    repaired = registry.invoke(
        ToolCall(id="synthetic_fixed", name="record_finding", arguments=args)
    )
    assert repaired.ok


def test_report_cannot_hide_unfinished_weather_behind_a_successful_query(agent_workspace):
    """
    作用：本地查询成功不抹掉天气失败，服务端在新报告保留未完成核查提示。
    输入：合成查询及真实失败工具记录。
    输出：无。
    """
    config, store, _, _ = agent_workspace
    run = new_run(agent_workspace)
    _, query, reports = services(agent_workspace)
    registry = ToolRegistry(
        run,
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        query,
        config,
        threading.Event(),
    )
    result = registry.invoke(ToolCall(id="synthetic_sql", name="query_sql", arguments={"sql": SQL}))
    assert result.ok
    weather = ToolCall(
        id="synthetic_weather",
        name="query_historical_weather",
        arguments=dict(airport="ATL", start="2026-01-24", end="2026-01-26"),
    )
    execution = store.tool_start(run["id"], weather)
    store.tool_finish(
        run["id"],
        execution,
        weather.name,
        ToolResult(ok=False, summary="合成超时", error_code="EXTERNAL_TIMEOUT"),
        1,
    )
    draft = report_candidate([dict(role="tool", content=json.dumps(result.model_dump()))])["report"]
    prepared = reports.prepare(run, draft)
    assert any("天气核查未成功完成" in warning for warning in prepared["limitations"])


def test_bulk_weather_preserves_column_order_types_nulls_precision_and_unicode(tmp_path):
    """
    作用：批量结构的键排序不能改变列对应关系，空值、精度和中文定位保持原值。
    输入：合成正式采集结果。
    输出：无。
    """

    async def verify():
        """作用：构建并读取真实 DuckDB；输入：无；输出：无。"""
        capture = await weather_capture(tmp_path)
        capture["observations"][0]["locator"] = '行号：温度 "原值"'
        names, expected = weather_rows(capture, "synthetic_evidence")
        folder = tmp_path / "snapshot"
        result = await build_weather_database(
            folder, "synthetic_snapshot", "synthetic_evidence", capture
        )
        with duckdb.connect(str(folder / "analytics.duckdb"), read_only=True) as connection:
            actual = connection.execute(
                "SELECT * FROM v_weather_observations ORDER BY observed_at_utc"
            ).fetchall()
        wanted = [
            tuple([datetime.fromisoformat(row[0]), date.fromisoformat(row[1]), *row[2:]])
            for row in expected
        ]
        assert actual == wanted
        assert [c["name"] for c in result["schema"]["tables"][0]["columns"]] == names
        assert result["row_count"] == len(expected)

    asyncio.run(verify())


@pytest.mark.parametrize("code", ["EXTERNAL_TIMEOUT", "RUN_CANCELLED"])
def test_bulk_guard_rolls_back_mid_batch(tmp_path, code):
    """
    作用：超时/取消发生在批次间时终止构建，未完成事务不能成为可查询快照。
    输入：跨多个批次的合成观测。
    输出：无。
    """

    async def verify():
        """作用：真实写第一批后中止并核对回滚；输入：无；输出：无。"""
        capture = await weather_capture(tmp_path)
        first = capture["observations"][0]
        start = datetime.fromisoformat(first["observed_at"])
        capture["observations"] = [
            {**first, "observed_at": (start + timedelta(seconds=i)).isoformat()} for i in range(600)
        ]
        checks = []

        def stop():
            """作用：在第二批前模拟真实边界检查失败；输入：无；输出：无。"""
            checks.append(True)
            if len(checks) == 2:
                raise DataError(code)

        folder = tmp_path / "incomplete"
        with pytest.raises(DataError, match=code):
            await build_weather_database(folder, "synthetic", "synthetic", capture, check=stop)
        with duckdb.connect(str(folder / "analytics.duckdb")) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='observations'"
                ).fetchone()[0]
                == 0
            )
        assert len(checks) == 2

    asyncio.run(verify())


def test_timeout_during_evidence_transaction_cannot_publish(agent_workspace, monkeypatch):
    """
    作用：登记期间期限到达必须回滚证据、快照与绑定，而不是只返回失败。
    输入：合成来源及提交前失败的服务端检查。
    输出：无。
    """

    async def verify():
        """作用：执行正式冻结流程并在提交前中止；输入：无；输出：无。"""
        config, store, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, _, _ = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        publish = external.publish

        def fail_before_commit(run, payload, snapshot=None, **kwargs):
            """作用：让事务第二次检查失败；输入：正式准备数据；输出：无。"""
            checks = []

            def guard():
                """作用：模拟提交前耗尽时间；输入：无；输出：无。"""
                checks.append(True)
                if len(checks) == 2:
                    raise DataError("EXTERNAL_TIMEOUT")

            return publish(run, payload, snapshot, check=guard)

        monkeypatch.setattr(external, "publish", fail_before_commit)
        with pytest.raises(DataError, match="EXTERNAL_TIMEOUT"):
            await external.freeze_weather(run["id"], capture["id"])
        from sqlalchemy import text

        with store.engine.connect() as connection:
            for name in ("external_evidence", "external_snapshots", "external_run_bindings"):
                assert connection.execute(text("SELECT COUNT(*) FROM " + name)).scalar_one() == 0

    asyncio.run(verify())


@pytest.mark.parametrize("producer", [None, "synthetic_weather_call"])
@pytest.mark.parametrize("state", ["succeeded", "failed", "mismatched"])
def test_only_successful_weather_producer_can_authorize(agent_workspace, producer, state):
    """
    作用：新记录按调用身份核对成功返回；旧明确日期记录也不能由失败工具升级为成功证据。
    输入：合成冻结结果和真实工具状态表。
    输出：无。
    """

    async def verify():
        """作用：模拟成功、失败和错误返回身份；输入：无；输出：无。"""
        config, store, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, _, _ = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        call = ToolCall(
            id="synthetic_weather_call",
            name="query_historical_weather",
            arguments=dict(
                airport="ATL",
                start=capture["local_start"],
                end=capture["local_end"],
                fields=list(capture["coverage"]),
            ),
        )
        execution = store.tool_start(run["id"], call)
        payload = await external.freeze_weather(run["id"], capture["id"], producer_call_id=producer)
        result = ToolResult(
            ok=state != "failed",
            summary="明确合成的生产工具结果",
            error_code="EXTERNAL_TIMEOUT" if state == "failed" else None,
            data=dict(evidence_id=payload["id"] if state == "succeeded" else "synthetic_wrong"),
        )
        store.tool_finish(run["id"], execution, call.name, result, 1)
        if state == "succeeded":
            assert external.authorize(run, payload["id"])["id"] == payload["id"]
        else:
            with pytest.raises(DataError, match="EXTERNAL_TOOL_NOT_SUCCEEDED"):
                external.authorize(run, payload["id"])

    asyncio.run(verify())
