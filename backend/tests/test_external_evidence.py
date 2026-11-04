"""5.C 合成采集输入配合真实 SQLite/DuckDB/报告服务，验证外部证据权限与版本。"""

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_agent
from alembic import command
from alembic.config import Config
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, canonical_bytes
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.models import ArtifactRecord, QueryExecution
from metric_pilot.data.query import QueryService
from metric_pilot.external.advisories import (
    AdvisorySearch,
    read_external_source,
    search_official_advisories,
)
from metric_pilot.external.evidence import ExternalEvidenceService
from metric_pilot.external.http import SourceHTTP
from metric_pilot.external.weather import WeatherRequest, local_window, query_historical_weather
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.service import ReportService
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agent import new_run
from test_external_advisories import DAY, NOW, body, client, entry, listing
from test_external_weather import Pool
from test_investigations import enqueue, investigation

agent_workspace = test_agent.agent_workspace
SQL = """SELECT COUNT(*) AS observation_times, COUNT(temperature_c) AS temperature_times,
MIN(temperature_c) AS minimum_c, MAX(wind_speed_m_s) AS max_wind
FROM v_weather_observations"""


async def weather_capture(storage, temperature=13.3):
    """
    作用：经真实采集解析服务创建明确合成的 AWC 结果及原件。
    输入：隔离存储和测试温度。
    输出：采集结果，绝不访问公网。
    """
    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    request = WeatherRequest(airport="ATL", start="2026-11-02")
    _, _, _, start, _ = local_window(request, now)
    sample = [
        dict(
            icaoId="KATL",
            obsTime=int(start.timestamp()) + minute * 60,
            metarType="METAR",
            lat=33.62972,
            lon=-84.44223,
            temp=value,
            wspd=10,
            visib="10+",
            rawOb="SYNTHETIC TEST OBSERVATION",
        )
        for minute, value in [(30, temperature), (90, 20), (750, None)]
    ]
    http = SourceHTTP(storage, pool_factory=lambda: Pool((canonical_bytes(sample),)))
    return await query_historical_weather(request, storage, now=now, http=http)


async def advisory_capture(storage):
    """
    作用：使用合成 FAA 页面走正式候选登记和固定正文读取。
    输入：隔离目录。
    输出：固定正文标识及候选，绝不访问公网。
    """
    http, _ = client(storage, [listing([entry()])])
    result = await search_official_advisories(
        AdvisorySearch(airport="DFW", start=DAY), storage, http=http, now=NOW
    )
    http, _ = client(storage, [body(entry())])
    page = await read_external_source(result["items"][0]["source_ref"], storage, http=http)
    return page, result


def services(workspace):
    """
    作用：复用隔离元数据库、受限执行器和报告服务。
    输入：测试工作区。
    输出：外部、查询及报告服务。
    """
    config, store, _, _ = workspace
    query = QueryService(Catalog(store.engine, config.storage_dir), QueryExecutor(10, 1000))
    return ExternalEvidenceService(store.engine, config.storage_dir), query, ReportService(query)


def query_weather(query, run, snapshot_id, sql=SQL, cancel=None, on_started=None):
    """
    作用：以真实运行边界执行独立天气 SQL。
    输入：服务、真实运行、外部快照和可选失败条件。
    输出：实际 ToolResult。
    """
    ctx = RunContext(
        run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
    )
    return query.execute(ctx, sql, {}, cancel, on_started, external_snapshot_id=snapshot_id)


def draft(artifact_id, evidence_ids):
    """
    作用：只提交单元格引用和原文引用，不填报告数字。
    输入：实际查询和证据标识。
    输出：严格报告草稿。
    """
    return ReportDraft.model_validate(
        dict(
            schema_version="1",
            title="合成外部证据链测试",
            scope="测试范围；天气与公告样本不用于推断因果",
            blocks=[
                dict(
                    type="kpi",
                    id="temperature",
                    title="样本最低精确温度（°C）",
                    value_ref=dict(artifact_id=artifact_id, row=0, column="minimum_c"),
                    format="decimal_2",
                ),
                dict(
                    type="text",
                    id="note",
                    text_template="有效温度观测 {{n}} 个时刻。",
                    values=dict(n=dict(artifact_id=artifact_id, row=0, column="temperature_times")),
                    evidence_ids=evidence_ids,
                ),
            ],
            limitations=["合成数据，不代表真实气象结论"],
        )
    )


def test_weather_sql_and_advisory_report_real_services(agent_workspace):
    """
    作用：验证原件到外部快照、真实 SQL、ValueRef、正文引用和不可变报告的完整服务路径。
    输入：隔离运行。
    输出：无。
    """

    async def run_test():
        """
        作用：组合全部正式服务，不调用 LLM。
        输入：无。
        输出：无。
        """
        config, store, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, query, reports = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        weather = await external.freeze_weather(run["id"], capture["id"])
        assert (await external.freeze_weather(run["id"], capture["id"]))["id"] == weather["id"]
        page, _ = await advisory_capture(config.storage_dir)
        announcement = await external.freeze_advisory(run["id"], page["body_ref"])
        result = query_weather(query, run, weather["external_snapshot_id"])
        assert result.ok, result
        artifact = result.data["artifact"]
        assert artifact["snapshot_id"] == run["snapshot_id"]
        assert artifact["external_snapshot_id"] == weather["external_snapshot_id"]
        assert artifact["external_evidence_ids"] == [weather["id"]]
        assert artifact["query_purpose"] == "exploratory" and not artifact["metric_revision_ids"]
        prepared = reports.prepare(run, draft(artifact["id"], [announcement["id"]]))
        store.finish(run["id"], "completed", prepared_report=prepared)
        completed = store.read(run["id"])
        report = reports.read(completed["result"]["report_id"], completed["result"]["revision_id"])
        assert report.blocks[0].value.raw == 13.3
        assert len(report.external_snapshots) == 1
        evidence = reports.evidence(report.report_id, report.revision_id, "temperature")
        assert evidence.external_snapshots[0]["id"] == weather["external_snapshot_id"]
        assert evidence.sources[0].source_kind == "official_weather_observations"
        text = reports.evidence(report.report_id, report.revision_id, "note")
        source = next(s for s in text.sources if s.id == announcement["id"])
        assert "AAL ONLY" in source.content and source.locator["line_start"] == 1
        # 外部数据再次采集不会重算旧报告；原始文件被篡改也不能改写已冻结报告。
        before = report.content_hash
        await weather_capture(config.storage_dir, 19)
        raw = external.path(external.root / "raw", weather["fetches"][0]["sha256"] + ".raw")
        raw.write_bytes(b"tampered synthetic bytes")
        assert reports.read(report.report_id, report.revision_id).content_hash == before
        with pytest.raises(DataError, match="EXTERNAL_SOURCE_CHANGED"):
            external.authorize(completed, weather["id"])

    asyncio.run(run_test())


@pytest.mark.parametrize("change", ["value", "raw", "failed", "truncated", "fake_id"])
def test_bad_capture_cannot_become_evidence(agent_workspace, change):
    """
    作用：拒绝编辑数值、原件、失败/截断及伪造采集 ID。
    输入：隔离工作区和破坏方式。
    输出：无。
    """

    async def run_test():
        """
        作用：从采集层损坏输入，确认登记前被拒绝。
        输入：无。
        输出：无。
        """
        config, _, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, _, _ = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        identifier = capture["id"]
        if change == "value":
            capture["observations"][0]["fields"]["temperature"]["value"] = 999
        elif change in ("failed", "truncated"):
            capture["fetches"][0].update(
                status="failed" if change == "failed" else "succeeded",
                truncated=change == "truncated",
            )
        elif change == "raw":
            path = config.storage_dir / "external/fetches" / (capture["fetches"][0]["id"] + ".raw")
            path.write_bytes(b"tampered test")
        else:
            identifier = "weather_" + "0" * 32
        (config.storage_dir / "external/weather" / (capture["id"] + ".json")).write_bytes(
            canonical_bytes(capture)
        )
        with pytest.raises(DataError):
            await external.freeze_weather(run["id"], identifier)

    asyncio.run(run_test())


def test_search_candidate_is_not_body_evidence(agent_workspace):
    """
    作用：拒绝尚未读取正文的候选和伪造外部引用。
    输入：隔离运行。
    输出：无。
    """

    async def run_test():
        """
        作用：核对候选不能跳过正文读取和固定登记。
        输入：无。
        输出：无。
        """
        config, _, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, _, _ = services(agent_workspace)
        _, search = await advisory_capture(config.storage_dir)
        with pytest.raises(DataError):
            await external.freeze_advisory(run["id"], search["items"][0]["source_ref"])
        with pytest.raises(DataError):
            external.collect(run, ["xe_" + "0" * 32])

    asyncio.run(run_test())


def test_explicit_same_investigation_reuse_and_version_conflict(agent_workspace):
    """
    作用：旧成功运行可在同调查复用，显式新版本不能被旧产物偷偷覆盖。
    输入：隔离调查。
    输出：无。
    """

    async def run_test():
        """
        作用：执行两个真实运行及外部版本，核对跨运行和错版本权限。
        输入：无。
        输出：无。
        """
        config, store, _, _ = agent_workspace
        inv = investigation(agent_workspace)
        first = enqueue(agent_workspace, inv)
        store.claim()
        first = store.read(first["id"])
        external, query, reports = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        old = await external.freeze_weather(first["id"], capture["id"])
        result = query_weather(query, first, old["external_snapshot_id"])
        assert result.ok
        original_draft = draft(result.artifact_ids[0], [old["id"]])
        store.finish(
            first["id"], "completed", prepared_report=reports.prepare(first, original_draft)
        )
        from metric_pilot.agent.investigations import InvestigationStore

        second = enqueue(agent_workspace, InvestigationStore(store.engine).read(inv["id"]))
        store.claim()
        second = store.read(second["id"])
        assert reports.prepare(second, original_draft)["blocks"][0]["value"]["raw"] == 13.3
        # 查询旧快照需要对本次运行显式绑定，不能凭知道 ID 执行。
        assert not query_weather(query, second, old["external_snapshot_id"]).ok
        external.bind_existing(second["id"], old["id"])
        assert query_weather(query, second, old["external_snapshot_id"]).ok
        changed = await weather_capture(config.storage_dir, 19)
        with pytest.raises(DataError, match="EXTERNAL_VERSION_MISMATCH"):
            await external.freeze_weather(second["id"], changed["id"])
        # 新运行可显式选择新版本；之后不能在该运行里混用旧版产物。
        store.finish(second["id"], "completed")
        third = enqueue(agent_workspace, InvestigationStore(store.engine).read(inv["id"]))
        store.claim()
        third = store.read(third["id"])
        new = await external.freeze_weather(third["id"], changed["id"])
        assert new["id"] != old["id"] and new["external_snapshot_id"] != old["external_snapshot_id"]
        with pytest.raises(DataError, match="EXTERNAL_VERSION_MISMATCH"):
            reports.prepare(third, original_draft)
        outsider = new_run(agent_workspace)
        with pytest.raises(DataError, match="EXTERNAL_EVIDENCE_NOT_ALLOWED"):
            external.collect(outsider, [old["id"]])
        with pytest.raises(DataError):
            reports.prepare(outsider, original_draft)

    asyncio.run(run_test())


@pytest.mark.parametrize(
    "failure", ["bad_sql", "cancel", "truncated", "wrong_context", "tamper_snapshot"]
)
def test_external_query_and_report_guards(agent_workspace, failure):
    """
    作用：复用 SQL 防护与真实执行失败逻辑，截断或错上下文不能进入报告。
    输入：隔离运行和失败方式。
    输出：无。
    """

    async def run_test():
        """
        作用：执行失败/拒绝路径，核对无假成功产物。
        输入：无。
        输出：无。
        """
        config, _, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, query, reports = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        frozen = await external.freeze_weather(run["id"], capture["id"])
        snapshot = frozen["external_snapshot_id"]
        if failure == "bad_sql":
            result = query_weather(query, run, snapshot, "SELECT * FROM v_flights")
        elif failure == "cancel":
            cancel = threading.Event()
            result = query_weather(query, run, snapshot, cancel=cancel, on_started=cancel.set)
        elif failure == "wrong_context":
            result = query.execute(
                RunContext(run["id"], run["snapshot_id"], "forged", time.monotonic() + 30),
                SQL,
                {},
                external_snapshot_id=snapshot,
            )
        elif failure == "tamper_snapshot":
            path = external.path(external.root / "snapshots" / snapshot, "analytics.duckdb")
            with path.open("ab") as stream:
                stream.write(b"tampered test bytes")
            result = query_weather(query, run, snapshot)
        else:
            query.executor = QueryExecutor(10, 1)
            result = query_weather(query, run, snapshot, "SELECT * FROM v_weather_observations")
            assert result.ok and result.data["artifact"]["truncated"]
            with pytest.raises(DataError, match="REPORT_TRUNCATED_RESULT"):
                reports.prepare(run, draft(result.artifact_ids[0], [frozen["id"]]))
            return
        assert not result.ok and not result.artifact_ids
        with Session(query.catalog.engine) as session:
            execution = session.get(QueryExecution, result.data["execution_id"])
            assert execution.status in ("failed", "cancelled")
            assert (
                session.scalar(
                    select(ArtifactRecord).where(ArtifactRecord.execution_id == execution.id)
                )
                is None
            )

    asyncio.run(run_test())


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_failed_source_run_cannot_be_reused(agent_workspace, status):
    """
    作用：真实冻结和查询成功也不能把未完成旧运行变成跨运行证据。
    输入：隔离调查及旧运行终态。
    输出：无。
    """

    async def run_test():
        """
        作用：结束旧运行后由新运行尝试绑定，检查实际终态权限。
        输入：无。
        输出：无。
        """
        config, store, _, _ = agent_workspace
        from metric_pilot.agent.investigations import InvestigationStore

        inv = investigation(agent_workspace)
        first = enqueue(agent_workspace, inv)
        store.claim()
        external, query, reports = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        frozen = await external.freeze_weather(first["id"], capture["id"])
        first = store.read(first["id"])
        result = query_weather(query, first, frozen["external_snapshot_id"])
        assert result.ok
        store.finish(first["id"], status)
        inv = InvestigationStore(store.engine).read(inv["id"])
        second = enqueue(agent_workspace, inv)
        store.claim()
        second = store.read(second["id"])
        with pytest.raises(DataError, match="EXTERNAL_EVIDENCE_NOT_ALLOWED"):
            external.bind_existing(second["id"], frozen["id"])
        with pytest.raises(DataError):
            reports.prepare(second, draft(result.artifact_ids[0], [frozen["id"]]))

    asyncio.run(run_test())


def test_bound_and_missing_weather_values_are_not_exact_or_zero(agent_workspace):
    """
    作用：验证缺测温度不补零、界限能见度不混入精确统计、降水累计列不开放。
    输入：合成 AWC 原件经正式固定表。
    输出：无。
    """

    async def run_test():
        """
        作用：从真实查询单元格检查精确数值与界限、缺失的分离。
        输入：无。
        输出：无。
        """
        config, _, _, _ = agent_workspace
        run = new_run(agent_workspace)
        external, query, _ = services(agent_workspace)
        capture = await weather_capture(config.storage_dir)
        frozen = await external.freeze_weather(run["id"], capture["id"])
        result = query_weather(
            query,
            run,
            frozen["external_snapshot_id"],
            "SELECT COUNT(*), COUNT(temperature_c), MIN(temperature_c), "
            "COUNT(visibility_km_exact), COUNT(visibility_km_lower_bound) "
            "FROM v_weather_observations",
        )
        assert result.ok
        assert query.read_artifact(run["id"], result.artifact_ids[0])["rows"] == [
            [3, 2, 13.3, 0, 3]
        ]
        bad = query_weather(
            query,
            run,
            frozen["external_snapshot_id"],
            "SELECT SUM(precipitation) FROM v_weather_observations",
        )
        assert not bad.ok and not bad.artifact_ids

    asyncio.run(run_test())


def test_migration_preserves_existing_query_artifacts(agent_workspace):
    """
    作用：有真实旧查询及外键引用时迁移，不重建或删除原记录。
    输入：隔离工作区。
    输出：无。
    """
    _, store, _, _ = agent_workspace
    run = new_run(agent_workspace)
    _, query, _ = services(agent_workspace)
    result = query.execute(
        RunContext(
            run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 30
        ),
        "SELECT COUNT(*) AS n FROM v_flights",
        {},
    )
    assert result.ok
    before = query.read_artifact(run["id"], result.artifact_ids[0])
    alembic = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    # 外部列全部为空，回到既有 0006 结构再原地升级，保留查询父子记录。
    store.engine.dispose()
    command.downgrade(alembic, "0006_investigations")
    command.upgrade(alembic, "head")
    command.check(alembic)
    assert query.read_artifact(run["id"], result.artifact_ids[0]) == before
