"""真实模型调查验收；运行实际工具，不审核正式指标或发布 Wiki。"""

import argparse
import asyncio
import json
import threading
from pathlib import Path

from metric_pilot.agent import models as agent_models
from metric_pilot.agent.contracts import CreateInvestigation, CreateRun
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.agent.store import RunStore
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import utc_now
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.service import ReportService
from metric_pilot.settings import Settings
from sqlalchemy import select
from verify_reports import baseline, values


def check_independent_counts(frozen, finding_evidence, reference, counts):
    """
    作用：独立核对固定发现和报告中的总数与逐日记录，不限制报告布局
    输入：冻结报告、发现依据、原 CSV 总量及逐日计数
    输出：实际核对摘要；不匹配时抛错
    """
    referenced = [value["raw"] for block in frozen["blocks"] for value in values(block)]
    artifacts = dict(frozen["artifacts"])
    for evidence in finding_evidence:
        referenced.extend(value["raw"] for value in values(evidence["block"]))
        artifacts.update(evidence["artifacts"])
    if reference["rows"] not in referenced:
        raise RuntimeError("INDEPENDENT_TOTAL_NOT_REFERENCED")
    matched = set()
    for item in artifacts.values():
        rows = item["rows"]
        # 对齐业务日期后比较整列，不能因偶然相同的某个数值而判定正确。
        for day_col, column in enumerate(item["artifact"]["columns"]):
            if column["type"] != "DATE" or len(rows) != len(counts):
                continue
            days = [
                row[day_col].get("value") if isinstance(row[day_col], dict) else row[day_col]
                for row in rows
            ]
            if len(set(days)) != len(counts) or set(days) != set(counts):
                continue
            for count_col in range(len(item["artifact"]["columns"])):
                if count_col != day_col and all(
                    type(row[count_col]) is int and row[count_col] == counts[day]
                    for row, day in zip(rows, days, strict=True)
                ):
                    matched.update(days)
    if matched != set(counts):
        raise RuntimeError("INDEPENDENT_DAILY_COUNTS_MISMATCH")
    return {"total_matches": True, "daily_dates_matched": len(matched)}


async def verify(resume=False):
    """
    作用：真实调查、继续核查和缺数据验收并保存原始结果
    输入：本机已有配置与真实数据
    输出：是否完成验收
    """
    config = Settings()
    record = {
        "created_at": utc_now(),
        "scope": "stage4_investigation_not_accuracy_benchmark",
        "passed": False,
        "cases": [],
    }
    target = config.storage_dir / "validation" / "stage4-investigations.json"
    previous = json.loads(target.read_text(encoding="utf-8")) if resume else None
    record["reused_existing_completed_runs"] = resume
    engine = create_metadata_engine(config)
    try:
        if not config.integration_configured("llm"):
            raise RuntimeError("MODEL_NOT_CONFIGURED")
        with engine.connect() as connection:
            if connection.execute(
                select(agent_models.runs.c.id).where(
                    agent_models.runs.c.status.in_(["queued", "running"])
                )
            ).first():
                raise RuntimeError("VALIDATION_REQUIRES_IDLE_WORKSPACE")
        catalog = Catalog(engine, config.storage_dir)
        source = next(
            s
            for s in catalog.sources()
            if s.source_kind == "official_bts" and s.current_snapshot_id
        )
        snapshot = catalog.snapshot(source.current_snapshot_id)
        reference, counts = baseline(Path("data/bts/2026-01.csv"), snapshot.period)
        if snapshot.file_hash != reference["file_hash"]:
            raise RuntimeError("BASELINE_HASH_MISMATCH")
        budget = {
            "max_turns": config.run_max_turns,
            "max_tool_calls": config.run_max_tool_calls,
            "timeout_seconds": config.run_timeout_seconds,
            "context_tokens": config.run_context_tokens,
            "output_tokens": config.run_output_tokens,
            "sql_timeout_seconds": config.sql_timeout_seconds,
            "sql_max_rows": config.sql_max_rows,
        }
        store, investigations = RunStore(engine), InvestigationStore(engine)
        if previous:
            inv = investigations.read(previous["investigation_id"])
            if inv["snapshot_id"] != snapshot.id:
                raise RuntimeError("REUSE_SNAPSHOT_MISMATCH")
            conv, fixed = {"id": inv["conversation_id"]}, {"id": inv["knowledge_snapshot_id"]}
        else:
            conv = store.conversation()
            fixed = KnowledgeStore(engine).snapshot()
            inv = investigations.create(
                CreateInvestigation(
                    conversation_id=conv["id"],
                    snapshot_id=snapshot.id,
                    goal="调查本月航班记录在日期上的分布及值得进一步核查的差异",
                    scope="仅对固定月份做基础数据探索，不计算尚未审核的项目指标，不推断天气因果或利润",
                ),
                fixed["id"],
                budget,
            )
        record["investigation_id"] = inv["id"]
        query = QueryService(
            catalog, QueryExecutor(config.sql_timeout_seconds, config.sql_max_rows)
        )
        reports = ReportService(query)
        first_artifacts = set()
        questions = [
            "请调查本月航班记录的日期分布。先查看整体记录与日期范围及逐日分布，再根据实际结果选择值得核查的日期或比较继续检查。"
            "这里只做记录条数的基础探索，不计算待审核的项目业务指标。不要求一定发现异常；说明样本、比较范围和限制。保存发现与待核查问题，再给出简洁中文报告。",
            "继续同一调查：先用 read_investigation 回顾发现，"
            "用 read_artifact 复核上一轮的冻结结果，保留已经核实的范围信息。"
            "然后根据现有证据补充一项有价值的检查，或用反证修正此前解释；不要把记录数差异解释成运行质量或天气原因。保存本轮发现，报告中引用上一轮仍适用的冻结证据并说明限制。",
            "继续同一调查，仅复核首轮已经核实的整月记录数和覆盖日期，不做新的业务计算。"
            "请先 read_investigation 找到首轮范围核对的发现，再 read_artifact 读取其冻结产物；"
            "本轮不要调用 query_sql 重新执行查询。使用这些旧产物的 ValueRef 保存简短核查记录，"
            "并以相同旧 ValueRef 生成简短中文报告，明确本轮没有新增查询或原因证据。",
            "在这个调查基础上，解释各航空公司的利润下降了多少。缺收入、成本和跨月数据时请明确说明不能判断，不要用航班记录数量替代利润。",
        ]
        for index, question in enumerate(questions):
            case_inv = inv
            if index == 3:
                # 改变业务目标时显式建立独立分支，不扩充或重置原调查额度。
                if previous and previous.get("profit_investigation_id"):
                    case_inv = investigations.read(previous["profit_investigation_id"])
                else:
                    case_inv = investigations.create(
                        CreateInvestigation(
                            conversation_id=conv["id"],
                            snapshot_id=snapshot.id,
                            parent_id=inv["id"],
                            goal="核查现有数据能否支持航空公司利润变化分析",
                            scope="检查收入、成本和跨月数据是否可用；缺失则说明不能判断",
                        ),
                        fixed["id"],
                        budget,
                    )
                record["profit_investigation_id"] = case_inv["id"]
            current = investigations.read(case_inv["id"])
            old_case = (
                next(
                    (
                        case
                        for case in previous["cases"]
                        if case["index"] == index + 1 and case["run"]["status"] == "completed"
                    ),
                    None,
                )
                if previous
                else None
            )
            if old_case:
                run = store.read(old_case["run"]["id"])
                if (
                    run["question"] != question
                    or run["investigation_id"] != case_inv["id"]
                    or run["knowledge_snapshot_id"] != fixed["id"]
                ):
                    raise RuntimeError("REUSE_INPUT_MISMATCH")
            else:
                run = store.create(
                    CreateRun(
                        conversation_id=conv["id"],
                        snapshot_id=snapshot.id,
                        question=question,
                        investigation_id=case_inv["id"],
                        expected_investigation_version=current["version"],
                    ),
                    fixed["id"],
                    budget,
                    config.llm_model,
                )
                claimed = store.claim()
                if claimed is None or claimed["id"] != run["id"]:
                    raise RuntimeError("OTHER_RUN_QUEUED_USE_IDLE_WORKSPACE")
                await AgentRuntime(store, config, AgentProvider(config)).run(
                    claimed, threading.Event()
                )
            run = store.read(run["id"])
            events, after = [], 0
            while True:
                batch = store.events_after(run["id"], after)
                events.extend(batch)
                if len(batch) < 100:
                    break
                after = batch[-1]["seq"]
            case = {
                "index": index + 1,
                "passed": False,
                "run": run,
                "events": events,
                "investigation": investigations.read(case_inv["id"]),
            }
            record["cases"].append(case)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if run["status"] != "completed":
                raise RuntimeError(run["error_code"] or "RUN_NOT_COMPLETED")
            if index < 3:
                if run["result"]["kind"] != "report":
                    raise RuntimeError("REPORT_REQUIRED")
                result = run["result"]
                frozen, _ = reports.frozen(result["report_id"], result["revision_id"])
                case["report"] = reports.read(
                    result["report_id"], result["revision_id"]
                ).model_dump()
                for item in frozen["artifacts"].values():
                    page = query.read_artifact(
                        item["artifact"]["run_id"], item["artifact"]["id"], 0, 100
                    )
                    if page["artifact"]["result_hash"] != item["artifact"]["result_hash"]:
                        raise RuntimeError("FROZEN_HASH_MISMATCH")
                if index == 0:
                    if not any(e["payload"].get("tool") == "query_sql" for e in events):
                        raise RuntimeError("REAL_QUERY_REQUIRED")
                    evidence = [
                        investigations.evidence(inv["id"], finding["id"])
                        for finding in case["investigation"]["findings"]
                        if finding["run_id"] == run["id"]
                    ]
                    case["independent_counts"] = check_independent_counts(
                        frozen, evidence, reference, counts
                    )
                    first_artifacts = set(frozen["artifacts"]) | {
                        key for item in evidence for key in item["artifacts"]
                    }
                else:
                    old_reads = [
                        e["payload"]["data"]["artifact"]["id"]
                        for e in events
                        if e["type"] == "tool.finished"
                        and e["payload"].get("tool") == "read_artifact"
                        and e["payload"].get("ok")
                        and e["payload"]["data"]["artifact"]["run_id"] != run["id"]
                    ]
                    case["cross_run_artifacts_read"] = old_reads
                    case["prior_artifacts_in_report"] = sorted(
                        first_artifacts & set(frozen["artifacts"])
                    )
                    if not old_reads:
                        raise RuntimeError("CROSS_RUN_ARTIFACT_READ_REQUIRED")
                    if index == 2:
                        if not case["prior_artifacts_in_report"]:
                            raise RuntimeError("CONTINUATION_EVIDENCE_NOT_REUSED")
                        if any(
                            e["type"] == "tool.started" and e["payload"].get("tool") == "query_sql"
                            for e in events
                        ):
                            raise RuntimeError("REUSE_ONLY_CASE_EXECUTED_NEW_QUERY")
                if not any(f["run_id"] == run["id"] for f in case["investigation"]["findings"]):
                    raise RuntimeError("FINDING_REQUIRED")
            elif run["result"]["kind"] != "clarification":
                raise RuntimeError("MISSING_DATA_CLARIFICATION_REQUIRED")
            case["passed"] = True
            print(json.dumps({"case": index + 1, "passed": True, "run_id": run["id"]}), flush=True)
        record["passed"] = True
        record["independent_reference"] = reference
        record["daily_count_days"] = len(counts)
    except Exception as error:
        record["error_code"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
    finally:
        engine.dispose()
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(record, ensure_ascii=False, indent=2)
        target.write_text(text, encoding="utf-8")
        target.with_name(
            "stage4-investigations-" + record["created_at"].replace(":", "-") + ".json"
        ).write_text(text, encoding="utf-8")
        print(
            json.dumps({"passed": record["passed"], "error_code": record.get("error_code")}),
            flush=True,
        )
    return record["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume-record", action="store_true", help="复核已有成功运行，仅为缺少的案例请求模型"
    )
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args().resume_record)) else 1)
