"""阶段 4 真实模型验收；仅比较当前固定数据，不审核正式指标或发布 Wiki。"""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from metric_pilot.data.common import utc_now
from metric_pilot.settings import Settings
from verify_agent import Client
from verify_bts_snapshot import checksum, unpack

QUESTIONS = {
    "range": (
        "这份航班数据共有多少条记录，覆盖哪些日期？请给出简洁的中文报告。"
        "这里只做数据范围核对，不使用项目业务指标。"
    ),
    "daily": (
        "按航班日期统计本月每天的记录条数，用中文折线图展示，并保留可核对的数据表。"
        "这只是基础数据探索，不使用项目业务指标。"
    ),
    "profit": "根据本月航班数据计算每家航空公司的利润。",
    "metric": "按项目定义比较本月各航空公司的到达延误率，生成排名报告。",
}


def baseline(path, period):
    """
    作用：直接从 CSV 独立计算日期与记录数，不使用 Agent 的 SQL 作为标准答案
    输入：实际原文件及固定月份
    输出：实际摘要与逐日计数
    """
    counts = Counter()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            day = datetime.strptime(row["FL_DATE"], "%m/%d/%Y %I:%M:%S %p").date().isoformat()
            if day[:7] == period:
                counts[day] += 1
    return {
        "rows": sum(counts.values()),
        "first": min(counts),
        "last": max(counts),
        "days": len(counts),
        "file_hash": checksum(path),
    }, counts


def values(block):
    """
    作用：枚举报告中服务端物化的引用值
    输入：已保存块
    输出：值迭代器
    """
    if block.get("value"):
        yield block["value"]
    for segment in block.get("segments", []):
        if segment.get("value"):
            yield segment["value"]
    for row in block.get("rows", []):
        yield from row


def check_report(api, run, events, reference, counts, case):
    """
    作用：核对真实报告、事件和逐值证据，区分基础探索与未审核口径
    输入：真实 HTTP 结果与 CSV 基线
    输出：已核对报告
    """
    if run["status"] != "completed" or run["result"]["kind"] != "report":
        raise RuntimeError("REPORT_NOT_COMPLETED")
    result = run["result"]
    base = f"/reports/{result['report_id']}/revisions/{result['revision_id']}"
    report = api.request("GET", base)
    if report["validation_level"] != "references_validated":
        raise RuntimeError("INVALID_VALIDATION_LEVEL")
    if [e["type"] for e in events][-2:] != ["report.ready", "run.completed"]:
        raise RuntimeError("REPORT_EVENT_ORDER")
    if len({e["seq"] for e in events}) != len(events):
        raise RuntimeError("DUPLICATE_EVENT_SEQ")
    if report["data_snapshot"]["file_hash"] != reference["file_hash"]:
        raise RuntimeError("BASELINE_HASH_MISMATCH")
    all_values, cache = [], {}
    for block in report["blocks"]:
        for value in values(block):
            ref = value["value_ref"]
            key = (ref["artifact_id"], ref["row"])
            if key not in cache:
                cache[key] = api.request(
                    "GET",
                    f"/runs/{run['id']}/artifacts/{ref['artifact_id']}?offset={ref['row']}&limit=1",
                )
            page = cache[key]
            names = [column["name"] for column in page["artifact"]["columns"]]
            if (
                page["artifact"]["truncated"]
                or page["rows"][0][names.index(ref["column"])] != value["raw"]
            ):
                raise RuntimeError("FROZEN_VALUE_MISMATCH")
            all_values.append(unpack(value["raw"]))
        selection = {"block_id": block["id"]}
        if block["type"] in {"table", "chart"} and block["rows"]:
            selection.update(row=0, column=block["columns"][0]["name"])
        api.request("GET", base + "/evidence", params=selection)
    if case == "range":
        if reference["rows"] not in all_values:
            raise RuntimeError("RANGE_COUNT_MISMATCH")
        if not {reference["first"], reference["last"]} <= set(all_values):
            raise RuntimeError("RANGE_DATE_MISMATCH")
    elif case == "daily":
        charts = [
            block
            for block in report["blocks"]
            if block["type"] == "chart" and block["kind"] == "line"
        ]
        if not charts:
            raise RuntimeError("DAILY_CHART_MISSING")
        actual = {str(unpack(row[0]["raw"])): unpack(row[1]["raw"]) for row in charts[0]["rows"]}
        if actual != dict(counts):
            raise RuntimeError("DAILY_COUNTS_MISMATCH")
    return report


def verify(args):
    """
    作用：经正式 API 运行或复核真实模型报告，不伪称模型通过
    输入：本地服务、实际原文件及案例范围
    输出：验收记录
    """
    settings = Settings()
    target = settings.storage_dir / "validation" / "stage4-reports.json"
    api = Client(args.base)
    report = {
        "created_at": utc_now(),
        "passed": False,
        "cases": [],
        "scope": "stage4_examples_not_accuracy_benchmark",
        "reused": args.reuse_record,
    }
    previous = json.loads(target.read_text(encoding="utf-8")) if args.reuse_record else None
    try:
        sources = api.request("GET", "/sources")["items"]
        source = next(item for item in sources if item["source_kind"] == "official_bts")
        snapshots = api.request("GET", f"/sources/{source['id']}/snapshots")["items"]
        snapshot = next(item for item in snapshots if item["id"] == source["current_snapshot_id"])
        reference, counts = baseline(args.input, snapshot["period"])
        if reference["file_hash"] != snapshot["file_hash"]:
            raise RuntimeError("INPUT_SNAPSHOT_HASH_MISMATCH")
        report.update(snapshot_id=snapshot["id"], baseline=reference)
        cases = QUESTIONS if args.case == "all" else {args.case: QUESTIONS[args.case]}
        for name, question in cases.items():
            if args.reuse_record:
                old = next(item for item in previous["cases"] if item["name"] == name)
                run = api.request("GET", f"/runs/{old['run']['id']}")
                events = api.request("GET", f"/runs/{run['id']}/event-log")["items"]
                if run["question"] != question or run["snapshot_id"] != snapshot["id"]:
                    raise RuntimeError("REUSE_INPUT_MISMATCH")
            else:
                conversation = api.request("POST", "/conversations")
                run, events = api.run(conversation["id"], snapshot["id"], question)
            case = {"name": name, "passed": False, "run": run, "events": events}
            report["cases"].append(case)
            if name in {"range", "daily"}:
                case["report"] = check_report(api, run, events, reference, counts, name)
            else:
                if run["status"] != "completed" or run["result"]["kind"] != "clarification":
                    raise RuntimeError("CLARIFICATION_REQUIRED")
                if any(e["type"] == "report.ready" for e in events):
                    raise RuntimeError("UNSUPPORTED_REPORT_CREATED")
            case["passed"] = True
            print(json.dumps({"case": name, "passed": True, "run_id": run["id"]}), flush=True)
        report["passed"] = all(item["passed"] for item in report["cases"])
    except Exception as error:
        report["error_code"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
    finally:
        target.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps(report, ensure_ascii=False, indent=2)
        target.write_text(record, encoding="utf-8")
        archive = target.with_name(
            "stage4-reports-" + report["created_at"].replace(":", "-") + ".json"
        )
        archive.write_text(record, encoding="utf-8")
        print(
            json.dumps({"passed": report["passed"], "error_code": report.get("error_code")}),
            flush=True,
        )
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--input", type=Path, default=Path("data/T_ONTIME_REPORTING.csv"))
    parser.add_argument("--case", choices=["all", *QUESTIONS], default="all")
    parser.add_argument("--reuse-record", action="store_true")
    raise SystemExit(0 if verify(parser.parse_args()) else 1)
