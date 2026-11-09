"""6.C真实数据范围验收：独立CSV基线、实际查询与报告，不调用模型或外部来源。"""

import argparse
import csv
import hashlib
import json
import math
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from metric_pilot.agent.contracts import CreateInvestigation, CreateRun
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.store import RunStore
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.data.ranges import DateRange
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.service import ReportService
from metric_pilot.settings import Settings


def csv_baseline(directory, periods, selected):
    """
    作用：独立解析原始导出列，按请求范围计算月度对照值
    输入：原件目录、月份和左闭右开范围
    输出：按月统计与扫描前后原件哈希
    """
    months, hashes = {}, {}
    for period in periods:
        path = directory / (period + ".csv")
        with path.open("rb") as stream:
            before = hashlib.file_digest(stream, "sha256").hexdigest()
        count = cancelled = diverted = missing = delay_n = 0
        delay_sum = Decimal(0)
        days = defaultdict(int)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                when = datetime.strptime(row["FL_DATE"], "%m/%d/%Y %I:%M:%S %p").date().isoformat()
                if not selected.start <= when < selected.end_exclusive:
                    continue
                count += 1
                days[when] += 1
                cancelled += int(Decimal(row["CANCELLED"]))
                diverted += int(Decimal(row["DIVERTED"]))
                if row["ARR_DELAY"].strip():
                    delay_sum += Decimal(row["ARR_DELAY"])
                    delay_n += 1
                else:
                    missing += 1
        with path.open("rb") as stream:
            after = hashlib.file_digest(stream, "sha256").hexdigest()
        hashes[period] = {"before": before, "after": after}
        if count:
            months[period] = [
                period,
                count,
                cancelled,
                diverted,
                missing,
                float(delay_sum / delay_n) if delay_n else None,
                min(days),
                max(days),
            ]
        print(f"{period}: independent CSV range scanned", flush=True)
    return months, hashes


def verify(config, snapshot_id, directory, selected):
    """
    作用：以明确范围创建人工验收运行，实际执行SQL并冻结报告
    输入：隔离配置、快照、原件目录和请求日期
    输出：逐值比对、实际报告、范围拒绝与续查结果
    """
    engine = create_metadata_engine(config)
    try:
        catalog = Catalog(engine, config.storage_dir)
        snapshot = catalog.snapshot(snapshot_id)
        baseline, hashes = csv_baseline(directory, snapshot.periods, selected)
        expected_sources = {item["period"]: item["sha256"] for item in snapshot.manifest["sources"]}
        for period, values in hashes.items():
            values["snapshot_sha256"] = expected_sources[period]
        knowledge = KnowledgeStore(engine).snapshot()
        runs, invs = RunStore(engine), InvestigationStore(engine)
        budget = {"max_turns": 1, "max_tool_calls": 4, "timeout_seconds": 120}
        conv = runs.conversation()
        inv = invs.create(
            CreateInvestigation(
                conversation_id=conv["id"],
                snapshot_id=snapshot_id,
                goal="6.C真实CSV范围数值核验（非模型调查）",
                scope="独立原件与实际受控SQL对照",
                date_range=selected,
            ),
            knowledge["id"],
            budget,
        )
        created = runs.create(
            CreateRun(
                conversation_id=conv["id"],
                snapshot_id=snapshot_id,
                investigation_id=inv["id"],
                expected_investigation_version=inv["version"],
                question="按固定范围核对逐月统计；人工验收，不调用模型",
            ),
            knowledge["id"],
            budget,
            model="manual-validation-no-model",
        )
        run = runs.claim()
        if run["id"] != created["id"]:
            raise RuntimeError("ISOLATED_STORE_HAS_OTHER_QUEUED_RUN")
        query = QueryService(catalog, QueryExecutor(timeout_seconds=10, max_rows=100))
        ctx = RunContext(run["id"], snapshot_id, knowledge["id"], time.monotonic() + 100, selected)
        # 故意省略日期条件，验证由服务端范围视图生效，而不是脚本自己筛SQL。
        sql = """SELECT SUBSTRING(CAST(flight_date AS VARCHAR),1,7) AS month,
            COUNT(*) AS n, SUM(cancelled) AS cancelled, SUM(diverted) AS diverted,
            COUNT(*)-COUNT(arrival_delay) AS missing_arrival_delay,
            AVG(arrival_delay) AS mean_delay,
            CAST(MIN(flight_date) AS VARCHAR) AS first_date,
            CAST(MAX(flight_date) AS VARCHAR) AS last_date
            FROM v_flights GROUP BY month ORDER BY month"""
        result = query.execute(
            ctx,
            sql,
            {},
            query_purpose="exploratory",
            exploration_scope={
                "population": "服务端固定日期范围内的Reporting Carrier航班",
                "calculation": "逐月记录数、取消/备降、到达偏差空白与非空偏差均值",
                "missing_values": "空白不填零；均值忽略空白，负到达偏差保留",
            },
        )
        if not result.ok:
            raise RuntimeError(result.error_code)
        page = query.read_artifact(run["id"], result.artifact_ids[0])
        expected = [baseline[key] for key in sorted(baseline)]
        actual = page["rows"]
        comparisons = []
        for wanted, observed in zip(expected, actual, strict=True):
            checks = [
                math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
                if isinstance(a, float) and b is not None
                else a == b
                for a, b in zip(wanted, observed, strict=True)
            ]
            comparisons.append({"month": wanted[0], "checks": checks})
        outside = query.execute(
            ctx,
            "SELECT COUNT(*) AS n FROM v_flights WHERE flight_date<:start OR flight_date>=:end",
            {"start": selected.start, "end": selected.end_exclusive},
        )
        if not outside.ok:
            raise RuntimeError(outside.error_code)
        outside_rows = query.read_artifact(run["id"], outside.artifact_ids[0])["rows"]
        draft = ReportDraft.model_validate(
            {
                "schema_version": "1",
                "title": "6.C固定范围数值核验",
                "scope": f"{selected.start} 至 {selected.end_exclusive}（结束日不含）",
                "metric_revision_ids": [],
                "limitations": ["人工验收；未调用模型，不代表Agent语义验收。"],
                "blocks": [
                    {
                        "type": "table",
                        "id": "months",
                        "title": "范围内的月度统计",
                        "artifact_id": result.artifact_ids[0],
                        "columns": [
                            "month",
                            "n",
                            "cancelled",
                            "diverted",
                            "missing_arrival_delay",
                            "mean_delay",
                            "first_date",
                            "last_date",
                        ],
                    }
                ],
            }
        )
        service = ReportService(query)
        prepared = service.prepare(run, draft)
        runs.finish(run["id"], "completed", prepared_report=prepared)
        report_ids = runs.read(run["id"])["result"]
        report = service.read(report_ids["report_id"], report_ids["revision_id"])
        refreshed = invs.read(inv["id"])
        follow = runs.create(
            CreateRun(
                conversation_id=conv["id"],
                snapshot_id=snapshot_id,
                investigation_id=inv["id"],
                expected_investigation_version=refreshed["version"],
                question="人工验收范围继承；未调用模型",
            ),
            knowledge["id"],
            budget,
            model="manual-validation-no-model",
        )
        # 不启动后台Worker；验收只验证范围继承，显式结束该排队运行。
        runs.finish(follow["id"], "cancelled", error_code="VALIDATION_NO_MODEL_EXECUTION")
        passed = bool(comparisons) and all(all(m["checks"]) for m in comparisons)
        passed = passed and outside_rows == [[0]] and report.date_range == selected
        passed = passed and follow["date_range"] == selected.model_dump()
        passed = passed and not page["artifact"]["truncated"]
        passed = passed and all(
            m["before"] == m["after"] == m["snapshot_sha256"] for m in hashes.values()
        )
        return {
            "passed": passed,
            "snapshot_id": snapshot_id,
            "date_range": selected.model_dump(),
            "expected": expected,
            "actual": actual,
            "comparisons": comparisons,
            "source_hashes": hashes,
            "outside_rows": outside_rows,
            "artifact_ids": result.artifact_ids + outside.artifact_ids,
            "report": report_ids,
            "report_date_range": report.date_range.model_dump(),
            "followup_date_range": follow["date_range"],
            "model_called": False,
            "external_sources_called": False,
            "scope": "数据层和人工报告验收，未做模型完整调查。",
        }
    finally:
        engine.dispose()


def main():
    """
    作用：向明确选择的隔离存储写验收记录，结果文件独占创建
    输入：命令行存储、快照、日期、原件和输出路径
    输出：JSON验收结果与退出码
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--input-dir", type=Path, default=Path("data/bts"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end-exclusive", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("OUTPUT_ALREADY_EXISTS")
    selected = DateRange(start=args.start, end_exclusive=args.end_exclusive)
    result = verify(
        Settings(_env_file=None, storage_dir=args.storage_dir),
        args.snapshot_id,
        args.input_dir,
        selected,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
