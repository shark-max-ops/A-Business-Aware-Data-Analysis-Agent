"""5.C 离线复核已留存真实原件，经真实 SQL 和报告服务取值；不调用模型。"""

import argparse
import asyncio
import os
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from metric_pilot.agent.contracts import CreateInvestigation, CreateRun
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.store import RunStore
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import canonical_bytes, new_id, utc_now
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.models import DataSnapshot, DataSource
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.external.evidence import ExternalEvidenceService
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.service import ReportService
from metric_pilot.settings import Settings
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
SQL = """SELECT COUNT(*) AS observation_times,
COUNT(temperature_c) AS temperature_times,
MIN(temperature_c) AS minimum_c, MAX(temperature_c) AS maximum_c,
COUNT(wind_speed_m_s) AS wind_times, MAX(wind_speed_m_s) AS maximum_wind_m_s,
COUNT(visibility_km_exact) AS visibility_exact_times,
COUNT(visibility_km_lower_bound) AS visibility_bound_times
FROM v_weather_observations"""
BUDGET = dict(
    max_turns=16, max_tool_calls=24, timeout_seconds=180, context_tokens=32000, output_tokens=3000
)


def clone_primary(source_engine, target_engine, snapshot_id):
    """
    作用：只复制真实主快照元信息用于版本边界，不复制整表或旧运行。
    输入：原引擎、隔离引擎及明确主快照。
    输出：无；本脚本不执行 BTS 查询。
    """
    with source_engine.connect() as c:
        snapshot = dict(
            c.execute(select(DataSnapshot.__table__).where(DataSnapshot.id == snapshot_id))
            .mappings()
            .one()
        )
        source = dict(
            c.execute(select(DataSource.__table__).where(DataSource.id == snapshot["source_id"]))
            .mappings()
            .one()
        )
    with target_engine.begin() as c:
        c.execute(DataSource.__table__.insert().values(**source))
        c.execute(DataSnapshot.__table__.insert().values(**snapshot))


async def verify(args):
    """
    作用：在新隔离目录生成真实天气数字和公告引用报告，逐项复核并留存。
    输入：已经采集的天气/公告 ID；不接受来源 URL 或模型数字。
    输出：中文摘要与可回读验收记录。
    """
    original = Settings()
    original_root = original.storage_dir.resolve()
    folder = original_root / "validation" / new_id("stage5c")
    config = Settings(_env_file=None, storage_dir=folder)
    # 仅迁移新隔离数据库，不改变用户主数据库和旧报告。
    previous = os.environ.get("STORAGE_DIR")
    os.environ["STORAGE_DIR"] = str(folder)
    try:
        alembic = Config(str(ROOT / "alembic.ini"))
        command.upgrade(alembic, "head")
        command.check(alembic)
    finally:
        if previous is None:
            os.environ.pop("STORAGE_DIR", None)
        else:
            os.environ["STORAGE_DIR"] = previous
    source_engine, engine = create_metadata_engine(original), create_metadata_engine(config)
    try:
        clone_primary(source_engine, engine, args.snapshot_id)
        store = RunStore(engine)
        knowledge = KnowledgeStore(engine).snapshot()
        conv = store.conversation()
        inv = InvestigationStore(engine).create(
            CreateInvestigation(
                conversation_id=conv["id"],
                snapshot_id=args.snapshot_id,
                goal="核对外部天气与公告证据链",
                scope="仅验证来源取值，不判断航班异常原因",
            ),
            knowledge["id"],
            BUDGET,
        )
        created = store.create(
            CreateRun(
                conversation_id=conv["id"],
                snapshot_id=args.snapshot_id,
                question="核对同机场同日的已固定天气与公告；不作因果结论",
                investigation_id=inv["id"],
                expected_investigation_version=inv["version"],
            ),
            knowledge["id"],
            BUDGET,
        )
        store.claim()
        run = store.read(created["id"])
        external = ExternalEvidenceService(engine, folder, import_storage=original_root)
        weather = await external.freeze_weather(run["id"], args.weather_id)
        notice = await external.freeze_advisory(run["id"], args.body_ref)
        airport = weather["document"]["mapping"]["airport"]
        day = weather["document"]["local_start"]
        assert notice["candidate"]["facility"].split("/")[0] == airport
        assert notice["candidate"]["utc_date"] == day
        query = QueryService(Catalog(engine, folder), QueryExecutor(10, 1000))
        result = query.execute(
            RunContext(
                run["id"], run["snapshot_id"], run["knowledge_snapshot_id"], time.monotonic() + 60
            ),
            SQL,
            {},
            external_snapshot_id=weather["external_snapshot_id"],
        )
        assert result.ok, result
        artifact_id = result.artifact_ids[0]
        # 数字只指定 ValueRef；不把已有 coverage 摘要直接写进报告。
        blocks = [
            dict(
                type="kpi",
                id=column,
                title=title,
                value_ref=dict(artifact_id=artifact_id, row=0, column=column),
                format="decimal_2",
            )
            for column, title in [
                ("minimum_c", "最低有效精确温度（°C）"),
                ("maximum_c", "最高有效精确温度（°C）"),
                ("maximum_wind_m_s", "最大有效精确风速（m/s）"),
            ]
        ]
        blocks.append(
            dict(
                type="text",
                id="sources",
                text_template="温度可用 {{n}} 个观测时刻。公告原文含航司限制；仅用于来源核对。",
                values=dict(n=dict(artifact_id=artifact_id, row=0, column="temperature_times")),
                evidence_ids=[notice["id"]],
            )
        )
        draft = ReportDraft(
            schema_version="1",
            title=f"{airport} 天气与公告固定证据核对",
            scope=f"{day} 机场当地天气日；公告按 UTC 签发日核对，二者不是同一时间范围",
            blocks=blocks,
            limitations=["未执行 BTS 查询、模型调查或因果判断；降水与积雪不计算累计量"],
        )
        reports = ReportService(query)
        prepared = reports.prepare(run, draft)
        store.finish(run["id"], "completed", prepared_report=prepared)
        completed = store.read(run["id"])
        report = reports.read(completed["result"]["report_id"], completed["result"]["revision_id"])
        page = query.read_artifact(run["id"], artifact_id)
        values = dict(
            zip([c["name"] for c in page["artifact"]["columns"]], page["rows"][0], strict=True)
        )
        for block in report.blocks[:3]:
            assert block.value.raw == values[block.id]
        coverage = weather["document"]["coverage"]
        assert values["minimum_c"] == coverage["temperature"]["exact_min"]
        assert values["maximum_c"] == coverage["temperature"]["exact_max"]
        assert values["maximum_wind_m_s"] == coverage["wind_speed"]["exact_max"]
        assert values["temperature_times"] == coverage["temperature"]["accepted_observation_times"]
        evidence = reports.evidence(report.report_id, report.revision_id, "sources")
        official = next(s for s in evidence.sources if s.id == notice["id"])
        assert official.content == notice["document"]["text"]
        assert official.locator["line_start"] == 1
        assert len(report.external_snapshots) == 1
        record = dict(
            passed=True,
            checked_at=utc_now(),
            model_calls=0,
            sources_refetched=False,
            weather_id=args.weather_id,
            body_ref=args.body_ref,
            query_artifact=page["artifact"],
            query_values=values,
            report=report.model_dump(),
            evidence=evidence.model_dump(),
            scope="已留存真实原件复核；未执行模型或 BTS SQL，不代表 5.D 完成",
        )
        output = original_root / "validation" / "stage5c-external-evidence.json"
        output.write_bytes(canonical_bytes(record))
        lines = [
            f"5.C 真实原件证据链核对通过：{airport}，{day}",
            f"有效温度：{values['temperature_times']} 个观测时刻",
            f"温度范围：{values['minimum_c']} 至 {values['maximum_c']} °C",
            f"最大有效精确风速：{values['maximum_wind_m_s']} m/s",
            f"公告：#{notice['candidate']['number']:03d}；保留完整原文和航司限制",
            "数字已与实际查询单元格逐项核对，原文已与固定正文核对。",
            "本次未重新联网、未调用模型、未执行 BTS 查询；不证明因果。",
            "验收记录：storage/validation/stage5c-external-evidence.json",
        ]
        (folder / "试用说明.txt").write_text("\n".join(lines), encoding="utf-8")
        print("\n".join(lines))
    finally:
        source_engine.dispose()
        engine.dispose()


def main():
    """
    作用：提供明确 ID 输入的独立验收命令。
    输入：命令参数。
    输出：中文可核对摘要，不输出密钥。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weather-id", required=True)
    parser.add_argument("--body-ref", required=True)
    parser.add_argument("--snapshot-id", required=True)
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
