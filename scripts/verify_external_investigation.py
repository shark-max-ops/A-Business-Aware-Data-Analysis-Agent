"""5.D 真实模型、公开来源、固定证据、报告与续查验收；不审核指标。"""

import argparse
import asyncio
import json
import sqlite3
import threading
from pathlib import Path

from metric_pilot.agent import models as m
from metric_pilot.agent.contracts import CreateInvestigation, CreateRun
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.agent.store import RunStore
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import canonical_bytes, utc_now
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.service import ReportService
from metric_pilot.settings import Settings
from sqlalchemy import select
from verify_reports import values

QUESTIONS = [
    "请调查2026年1月25日 ATL 出发航班的取消情况。先核对当日全部记录数、取消记录数作为参照，"
    "再结合该机场当日真实历史天气观测及 FAA 官方运行公告正文核查。"
    "公告本次仅检索2026年1月25日UTC签发日；不额外增加其他机场或航司排名。"
    "本轮不拆分承运人、不展开逐小时表，只核对小时覆盖并呈现少量关键数字及来源限制。"
    "核对时间、覆盖缺失和公告航司限制；选少量天气数字通过查询和 ValueRef 呈现。"
    "航班与天气分别用一张一行的整体聚合表，列标题写清含义；解释文字不重复表中数量。"
    "公告仅准确引用已读原文的适用范围，附实际公告来源，不使用无关数字占位符。"
    "保存观察事实、候选解释或限制，给出简洁中文报告，不把同现当因果，不使用未审核项目指标。",
    "继续本调查：优先复用已完成运行的固定航班查询、天气观测及公告正文，"
    "本轮不重新联网。核对天气覆盖是否完整、公告是否适用于全部航司，并据此修正或限制之前的解释。"
    "通过已有成功查询的 ValueRef 保存本轮发现，再生成简短报告，不重复整段旧内容。",
    "请查询 ATL 机场当地昨天的实际天气观测，核对可用字段、时区和覆盖缺失。"
    "用成功查询产物的 ValueRef 呈现少量天气数字，保存核查发现和简短报告。"
    "不要拿一月航班快照去比较昨天的航班，不查询不相关公告，也不推断运行原因。",
]

WEATHER_FEEDBACK_QUESTIONS = [
    "调查本月 ATL 出发航班的取消情况。先比较记录数和取消分布，根据实际结果选择值得核查的日期，"
    "再结合天气观测和 FAA 运行公告核对。说明时间、缺测及航司限制，"
    "保存发现和简短报告，不直接推断原因。"
    "这里只做探索，不冒充已审核项目指标。",
    "优先复用刚才的固定证据，检查天气覆盖是否完整、公告是否适用于全部航司。"
    "保存支持或削弱此前判断的新发现，并说明还有哪些证据不足。",
]


async def verify(args):
    """
    作用：运行真实模型选择工具，并校验来源、产物和报告值，不预设 SQL 或结论。
    输入：是否恢复已有成功案例和显式 DNS 模式。
    输出：中文进度及忽略的完整验收记录。
    """
    config = Settings()
    if args.public_dns:
        config = config.model_copy(update={"external_dns_mode": "cloudflare"})
    if not config.integration_configured("llm"):
        raise RuntimeError("MODEL_NOT_CONFIGURED")
    target = config.storage_dir / (
        "validation/stage5-weather-repair.json"
        if args.repair_run
        else "validation/stage5-weather-feedback.json"
        if args.weather_feedback
        else "validation/stage5d-fresh.json"
        if args.fresh_main
        else "validation/stage5d-investigations.json"
    )
    record = (
        json.loads(target.read_text(encoding="utf-8"))
        if args.resume and target.exists()
        else dict(
            started_at=utc_now(), scope="真实案例验收，不是整体准确率评估", cases=[], passed=False
        )
    )
    record["automatic_checks_passed"] = False
    record["weather_pipeline_checks_passed"] = False
    record["source_acquisition"] = "live_source_requests"
    if not args.replay_weather_capture:
        record.pop("weather_capture_id", None)
    if args.replay_weather_capture:
        # 仅验收可显式回放原件；不改正式客户端、缓存期限或原获取时间。
        import metric_pilot.agent.external_tools as external_tools

        capture = json.loads(
            (
                config.storage_dir / "external/weather" / (args.replay_weather_capture + ".json")
            ).read_text(encoding="utf-8")
        )

        async def replay(request, storage, **kwargs):
            """作用：显式回放真实旧采集；输入：模型请求；输出：未经改写的旧采集。"""
            assert request.airport == capture["mapping"]["airport"]
            assert str(request.start) == capture["local_start"]
            assert str(request.end) == capture["local_end"]
            return capture

        external_tools.query_historical_weather = replay
        record["source_acquisition"] = "explicit_archived_capture_replay_not_live_download"
        record["weather_capture_id"] = args.replay_weather_capture
    # 修复复测使用元数据库副本，保留原调查预算、历史和旧报告。
    if args.repair_run:
        isolated = Path(".cache") / ("weather-repair-" + utc_now().replace(":", "-"))
        isolated.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(config.storage_dir / "metadata.sqlite") as original_db:
            with sqlite3.connect(isolated / "metadata.sqlite") as copied_db:
                original_db.backup(copied_db)
        engine = create_metadata_engine(config.model_copy(update={"storage_dir": isolated}))
        record["metadata_isolation"] = str(isolated)
        record["immutable_source_storage_shared"] = True
    else:
        engine = create_metadata_engine(config)
    store, investigations = RunStore(engine), InvestigationStore(engine)
    try:
        with engine.connect() as c:
            if c.execute(
                select(m.runs.c.id).where(m.runs.c.status.in_(["running", "queued"]))
            ).first():
                raise RuntimeError("VALIDATION_REQUIRES_IDLE_WORKSPACE")
        catalog = Catalog(engine, config.storage_dir)
        source = next(
            s
            for s in catalog.sources()
            if s.source_kind == "official_bts" and s.current_snapshot_id
        )
        snapshot = catalog.snapshot(source.current_snapshot_id)
        query = QueryService(
            catalog, QueryExecutor(config.sql_timeout_seconds, config.sql_max_rows)
        )
        reports = ReportService(query)
        budget = dict(
            max_turns=config.run_max_turns,
            max_tool_calls=config.run_max_tool_calls,
            timeout_seconds=config.run_timeout_seconds,
            context_tokens=config.run_context_tokens,
            output_tokens=config.run_output_tokens,
            sql_timeout_seconds=config.sql_timeout_seconds,
            sql_max_rows=config.sql_max_rows,
            external_max_requests=config.external_max_requests,
            external_max_seconds=config.external_max_seconds,
            external_max_bytes=config.external_max_bytes,
        )
        old_sources = set()
        baseline_artifacts = set()
        selected = list(enumerate(QUESTIONS[:1] if args.fresh_main else QUESTIONS))
        if args.weather_feedback:
            selected = list(enumerate(WEATHER_FEEDBACK_QUESTIONS))
        if args.repair_run:
            original = store.read(args.repair_run)
            assert original["status"] == "completed" and original["investigation_id"]
            record["investigation_id"] = original["investigation_id"]
            record["baseline_run_id"] = original["id"]
            baseline, _ = reports.frozen(
                original["result"]["report_id"], original["result"]["revision_id"]
            )
            baseline_artifacts = set(baseline["artifacts"])
            snapshot = catalog.snapshot(original["snapshot_id"])
            selected = [
                (
                    1,
                    "复测之前失败的天气步骤：重新查询 ATL 当地2026年1月24日至26日的历史天气，"
                    "检查温度、风速、能见度、降水和雪深字段。旧天气工具超时，不能把其遗留来源视为成功证据。"
                    "用新的成功天气版本执行一次整体聚合，核对总观测数、不同UTC日期小时数以及可用测量数，"
                    "明确当地日期和UTC起止范围。本轮只核查天气，不新增公告或航班查询，"
                    "不展示或声称引用旧航班统计；旧航班报告保持不变。"
                    "保存天气覆盖发现，给简短中文报告，所有数量用ValueRef，不把未查询日期说成来源无资料。",
                )
            ]
        if args.recheck:
            baseline = next(
                c
                for c in reversed(record["cases"])
                if c["index"] == 0 and c["run"]["status"] == "completed"
            )
            record["investigation_id"] = baseline["run"]["investigation_id"]
            old_sources = set(baseline["source_ids"])
            selected = [
                (
                    1,
                    "复核本调查已有发现与报告中的数量和公告解释。优先使用固定旧证据，"
                    "不要重新联网。逐项核对数值引用的列、分组范围和原文是否支持表述；"
                    "尤其区分全日有效测量数、总观测数和单个小时的测量数。"
                    "复核公告的目标机场、到达或出发方向及航司限制；不能将未直接覆盖等同于完全不相关。"
                    "必要时对已选定旧天气版本执行新的整体聚合。"
                    "对错误保存反证或修正，使用supersedes保留历史，最后给简短中文报告，"
                    "只保留可核对的数量和解释，不推断因果。",
                )
            ]
        for index, question in selected:
            if args.replay_weather_capture:
                question += (
                    "本次是明确的离线原件回放验收：天气工具使用此前真实下载的固定原件，"
                    "重新核验并冻结，不是刚刚联网下载；报告必须写明这一点。refresh不要设为true。"
                )
            previous = next(
                (c for c in record["cases"] if c["index"] == index and c.get("passed")), None
            )
            if previous and not args.new_case:
                if index == 0:
                    old_sources = set(previous["source_ids"])
                continue
            reusable = (
                next(
                    (
                        c
                        for c in reversed(record["cases"])
                        if c["index"] == index and c["run"]["status"] == "completed"
                    ),
                    None,
                )
                if args.resume and index == 0 and not args.fresh_main and not args.weather_feedback
                else None
            )
            if reusable:
                inv = investigations.read(reusable["run"]["investigation_id"])
                record["investigation_id"] = inv["id"]
                question = (
                    "继续本调查，优先回顾并复用已经成功的航班与固定天气证据，不重新联网查天气。"
                    "补齐2026年1月25日 UTC 签发的 ATL FAA 公告正文核查；本轮先仅核对该签发日。"
                    "核对航司限制并保存本轮发现，"
                    "报告引用仍适用的航班统计与天气 ValueRef 和公告正文，"
                    "说明当地日期/UTC与覆盖边界，不推断因果。"
                )
            elif index in (0, 2):
                conv = store.conversation()
                fixed = KnowledgeStore(engine).snapshot()
                inv = investigations.create(
                    CreateInvestigation(
                        conversation_id=conv["id"],
                        snapshot_id=snapshot.id,
                        goal=question,
                        scope="明确区分当地天气日、公告UTC时段和固定航班月份，不证明因果",
                    ),
                    fixed["id"],
                    budget,
                )
                record["recent_investigation_id" if index == 2 else "investigation_id"] = inv["id"]
            else:
                inv = investigations.read(record["investigation_id"])
            run = store.create(
                CreateRun(
                    conversation_id=inv["conversation_id"],
                    snapshot_id=snapshot.id,
                    question=question,
                    investigation_id=inv["id"],
                    expected_investigation_version=inv["version"],
                ),
                inv["knowledge_snapshot_id"],
                budget,
                config.llm_model,
            )
            claimed = store.claim()
            assert claimed and claimed["id"] == run["id"]
            print(f"真实案例 {index + 1} 开始", flush=True)
            await AgentRuntime(store, config, AgentProvider(config)).run(claimed, threading.Event())
            done = store.read(run["id"])
            events = store.events_after(run["id"], 0)
            case = dict(
                index=index,
                run=done,
                events=events,
                passed=False,
                source_acquisition=record["source_acquisition"],
                metadata_isolation=record.get("metadata_isolation"),
            )
            record["cases"].append(case)
            record.update(passed=False, finished_at=None)
            target.write_bytes(canonical_bytes(record))
            assert done["status"] == "completed" and done["result"]["kind"] == "report", done.get(
                "error_code"
            )
            frozen = reports.frozen(done["result"]["report_id"], done["result"]["revision_id"])
            p = frozen[0] if isinstance(frozen, tuple) else frozen
            findings = [
                f for f in investigations.read(inv["id"])["findings"] if f["run_id"] == run["id"]
            ]
            evidence = [investigations.evidence(inv["id"], f["id"]) for f in findings]
            artifacts = dict(p["artifacts"])
            sources = dict(p["sources"])
            for item in evidence:
                artifacts.update(item["artifacts"])
                sources.update(item["sources"])
            # 对每个实际 ValueRef 回读成功产物，核对物化值，而不是检验预设答案。
            checked = 0
            for block in [*p["blocks"], *[item["block"] for item in evidence]]:
                for value in values(block):
                    ref = value["value_ref"]
                    artifact = artifacts[ref["artifact_id"]]["artifact"]
                    page = query.read_artifact(artifact["run_id"], artifact["id"], ref["row"], 1)
                    columns = [c["name"] for c in page["artifact"]["columns"]]
                    assert page["rows"][0][columns.index(ref["column"])] == value["raw"]
                    assert not page["artifact"]["truncated"]
                    checked += 1
            assert findings and checked
            kinds = {s["source_kind"] for s in sources.values()}
            assert "official_weather_observations" in kinds
            if index == 0:
                assert "official_advisory" in kinds
                assert any(
                    not a["artifact"].get("external_snapshot_id") for a in artifacts.values()
                )
                old_sources = set(sources)
            if index == 1:
                if args.repair_run:
                    case["baseline_artifacts_reused"] = bool(baseline_artifacts & set(artifacts))
                    assert any(
                        e["type"] == "tool.finished"
                        and e["payload"].get("tool") == "query_historical_weather"
                        and e["payload"].get("ok")
                        for e in events
                    )
                else:
                    assert old_sources & set(sources)
            starts = [
                e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.started"
            ]
            ends = [
                e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.finished"
            ]
            assert starts == ends and len({e["seq"] for e in events}) == len(events)
            case.update(
                automatic_checks_passed=True,
                manual_review={"status": "pending"},
                source_ids=list(sources),
                source_kinds=sorted(kinds),
                checked_values=checked,
                report=p,
                finding_evidence=evidence,
            )
            target.write_bytes(canonical_bytes(record))
            print(
                f"真实案例 {index + 1} 自动检查通过，核对 {checked} 个引用值、"
                f"{len(sources)} 项来源；语义待逐项人工复核",
                flush=True,
            )
        record.update(
            passed=False,
            automatic_checks_passed=True,
            acceptance_status="pending_manual_review",
            finished_at=utc_now(),
        )
        target.write_bytes(canonical_bytes(record))
        print("记录：" + str(target.relative_to(config.storage_dir)), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--replay-weather-capture", help="仅验收：显式回放真实留存原件，不重新联网")
    parser.add_argument("--repair-run", help="从指定原调查用实际追问继续核查，保留旧报告")
    parser.add_argument("--public-dns", action="store_true")
    parser.add_argument("--fresh-main", action="store_true")
    parser.add_argument(
        "--weather-feedback", action="store_true", help="复测用户原始天气调查与继续追问"
    )
    parser.add_argument("--recheck", action="store_true", help="同调查复核旧报告和实际引用范围")
    parser.add_argument(
        "--new-case", action="store_true", help="保留记录并重新创建验收案例，不跳过已通过项"
    )
    asyncio.run(verify(parser.parse_args()))
