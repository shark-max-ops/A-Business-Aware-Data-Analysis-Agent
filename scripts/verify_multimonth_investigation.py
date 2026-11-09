"""6.E真实路径验收：独立原CSV基线、实际HTTP模型运行和冻结引用，不自动审核指标。"""

import argparse
import csv
import hashlib
import json
import math
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlencode

import sqlglot
from metric_pilot.data.common import utc_now
from sqlglot import exp
from verify_agent import Client
from verify_reports import values

FIELDS = (
    "record_count",
    "cancelled_count",
    "diverted_count",
    "missing_arrival_delay",
    "negative_scheduled_elapsed",
    "missing_scheduled_elapsed",
)


def checksum(path):
    """
    作用：计算实际文件指纹，不使用修改时间冒充来源
    输入：文件路径
    输出：SHA256
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, payload):
    """
    作用：持续保留本次验收进展和真实失败
    输入：本次独立结果文件及内容
    输出：无
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def baseline(api, snapshot_id, input_dir, store):
    """
    作用：独立读取BTS原始列，累计日期与承运人计数并绑定快照原件哈希
    输入：真实目录API、快照ID、原件目录、隔离存储
    输出：可按月/日/承运人重新汇总的基线
    """
    snapshots = [
        item
        for source in api.request("GET", "/sources")["items"]
        for item in api.request("GET", f"/sources/{source['id']}/snapshots")["items"]
    ]
    snapshot = next(item for item in snapshots if item["id"] == snapshot_id)
    daily = defaultdict(lambda: [0] * len(FIELDS))
    files, dates = {}, {}
    for source in snapshot["manifest"]["sources"]:
        period = source["period"]
        original = input_dir / f"{period}.csv"
        before = checksum(original)
        frozen = store / "datasets" / snapshot_id / source["file"]
        count = 0
        with original.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                raw_date = row["FL_DATE"]
                if raw_date not in dates:
                    dates[raw_date] = (
                        datetime.strptime(raw_date, "%m/%d/%Y %I:%M:%S %p").date().isoformat()
                    )
                day = dates[raw_date]
                if day[:7] != period:
                    raise RuntimeError("RAW_DATE_OUTSIDE_MONTH")
                group = daily[day + "|" + row["OP_UNIQUE_CARRIER"]]
                group[0] += 1
                group[1] += int(Decimal(row["CANCELLED"]))
                group[2] += int(Decimal(row["DIVERTED"]))
                group[3] += not row["ARR_DELAY"].strip()
                elapsed = row["CRS_ELAPSED_TIME"].strip()
                group[4] += bool(elapsed) and Decimal(elapsed) < 0
                group[5] += not elapsed
                count += 1
        after, frozen_hash = checksum(original), checksum(frozen)
        if not before == after == frozen_hash == source["sha256"]:
            raise RuntimeError("SOURCE_HASH_MISMATCH")
        files[period] = {
            "rows": count,
            "before": before,
            "after": after,
            "snapshot_sha256": frozen_hash,
        }
        print(json.dumps({"baseline_month": period, "rows": count}), flush=True)
    folder = store / "datasets" / snapshot_id
    protected = {
        str(path.relative_to(store)): checksum(path)
        for path in folder.rglob("*")
        if path.is_file() and path.suffix != ".csv"
    }
    return {
        "created_at": utc_now(),
        "snapshot_id": snapshot_id,
        "fields": FIELDS,
        "sources": files,
        "daily_carrier": dict(daily),
        "protected_snapshot_files": protected,
    }


def groups(reference, selected, grain, carrier=None):
    """
    作用：从独立日粒度基线重新汇总请求范围，不执行模型SQL作为正确答案
    输入：原CSV基线、范围、分组键和可选承运人
    输出：分组计数与百分比
    """
    result = defaultdict(lambda: [0] * len(FIELDS))
    observed_dates = defaultdict(set)
    for key, counts in reference["daily_carrier"].items():
        day, code = key.split("|")
        if not selected["start"] <= day < selected["end_exclusive"] or carrier and carrier != code:
            continue
        parts = {"month": day[:7], "day": day, "carrier_code": code}
        group_key = tuple(parts[name] for name in grain)
        group = result[group_key]
        observed_dates[group_key].add(day)
        for index, value in enumerate(counts):
            group[index] += value
    output = {}
    for key, counts in result.items():
        output[key] = dict(zip(FIELDS, counts, strict=True))
        output[key]["cancellation_rate"] = 100 * counts[1] / counts[0] if counts[0] else None
        # 独立扫描逐行强制解析CANCELLED，空值会使扫描失败；成功基线未接受任何空标记。
        output[key]["cancelled_null"] = 0
        output[key]["min_date"] = min(observed_dates[key])
        output[key]["max_date"] = max(observed_dates[key])
        output[key]["active_days"] = len(observed_dates[key])
    return output


def output_precision(sql, column):
    """
    作用：识别整列最终ROUND精度，不以宽松容差掩盖错误数值
    输入：实际SQL与结果列名
    输出：声明的小数位数或空值
    """
    for alias in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Alias):
        if alias.alias != column:
            continue
        node = alias.this
        if isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Round):
            digits = node.args.get("decimals")
            if digits is None:
                return 0
            if isinstance(digits, exp.Literal) and digits.is_int:
                precision = int(digits.this)
                if 0 <= precision <= 10:
                    return precision
    return None


def events(api, run_id):
    """
    作用：完整分页读取真实执行事件
    输入：API和运行ID
    输出：有序事件
    """
    result, cursor = [], 0
    while True:
        page = api.request("GET", f"/runs/{run_id}/event-log?after={cursor}")
        result.extend(page["items"])
        if not page["next_cursor"]:
            return result
        cursor = int(page["next_cursor"])


def read_report_evidence(api, report):
    """
    作用：从公开取证入口逐值检查并分页读取实际来源运行的成功产物
    输入：API与公开报告DTO
    输出：依据响应与冻结产物，不依赖内部数据库载荷
    """
    base = f"/reports/{report['report_id']}/revisions/{report['revision_id']}/evidence"
    records, artifacts = [], {}
    for block in report["blocks"]:
        selections = []
        if block.get("value"):
            selections.append(({}, block["value"]))
        for segment in block.get("segments", []):
            if segment.get("value"):
                selections.append(({"value_key": segment["key"]}, segment["value"]))
        for row in block.get("rows", []):
            for value in row:
                ref = value["value_ref"]
                selections.append(({"row": ref["row"], "column": ref["column"]}, value))
        for selection, value in selections:
            response = api.request(
                "GET", base + "?" + urlencode({"block_id": block["id"], **selection})
            )
            if response["value"] != value or response.get("date_range") != report["date_range"]:
                raise RuntimeError("PUBLIC_EVIDENCE_VALUE_MISMATCH")
            records.append(response)
            artifact = response["artifact"]
            if artifact["id"] in artifacts:
                continue
            rows, offset = [], 0
            while True:
                page = api.request(
                    "GET",
                    f"/runs/{artifact['run_id']}/artifacts/{artifact['id']}?offset={offset}&limit=100",
                )
                rows.extend(page["rows"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            if len(rows) != artifact["returned_rows"]:
                raise RuntimeError("ARTIFACT_PAGINATION_INCOMPLETE")
            artifacts[artifact["id"]] = {"artifact": artifact, "rows": rows}
    return records, artifacts


def run_case(api, investigation, question, metric_id, output):
    """
    作用：通过正式HTTP入口运行当前真实模型，按实际预算等待并保留失败
    输入：调查、自然语言目标、已审核指标ID和独立结果文件
    输出：完整运行、事件和报告
    """
    current = api.request("GET", f"/investigations/{investigation['id']}")
    started = time.monotonic()
    run = api.request(
        "POST",
        "/runs",
        json={
            "conversation_id": current["conversation_id"],
            "snapshot_id": current["snapshot_id"],
            "investigation_id": current["id"],
            "expected_investigation_version": current["version"],
            "question": question,
            "metric_revision_ids": [metric_id],
        },
    )
    case = {"created_at": utc_now(), "run": run, "passed": False}
    save(output, case)
    deadline = started + run["budget"]["timeout_seconds"] + 25
    while run["status"] in {"queued", "running"}:
        if time.monotonic() > deadline:
            api.request("POST", f"/runs/{run['id']}/cancel")
            raise RuntimeError("VALIDATION_WAIT_TIMEOUT")
        time.sleep(0.5)
        run = api.request("GET", f"/runs/{run['id']}")
    case.update(run=run, elapsed_seconds=time.monotonic() - started, events=events(api, run["id"]))
    inv = api.request("GET", f"/investigations/{current['id']}")
    case["findings"] = [item for item in inv["findings"] if item["run_id"] == run["id"]]
    if run["status"] == "completed" and run.get("result", {}).get("kind") == "report":
        r = run["result"]
        case["report"] = api.request(
            "GET", f"/reports/{r['report_id']}/revisions/{r['revision_id']}"
        )
        case["evidence"], case["artifacts"] = read_report_evidence(api, case["report"])
    save(output, case)
    print(
        json.dumps(
            {
                "run_id": run["id"],
                "status": run["status"],
                "result": run.get("result"),
                "error_code": run.get("error_code"),
                "elapsed": case["elapsed_seconds"],
            }
        ),
        flush=True,
    )
    return case


def check_case(api, case, reference, require_old=False, required_grain="month"):
    """
    作用：独立对齐冻结分组值、日期范围、正式口径、旧引用和逐值取证
    输入：实际案例、原CSV基线及旧证据要求
    输出：检查摘要，未覆盖的计算明确拒绝通过
    """
    run = case["run"]
    if run["status"] != "completed" or "report" not in case or not case["findings"]:
        raise RuntimeError("COMPLETED_REPORT_AND_FINDINGS_REQUIRED")
    report = case["report"]
    selected = run["date_range"]
    if report["date_range"] != selected:
        raise RuntimeError("REPORT_RANGE_CHANGED")
    actual_events = case["events"]
    if [e["seq"] for e in actual_events] != list(range(1, run["seq"] + 1)):
        raise RuntimeError("EVENT_REPLAY_MISMATCH")
    started = [
        e["payload"]["tool_execution_id"] for e in actual_events if e["type"] == "tool.started"
    ]
    finished = [
        e["payload"]["tool_execution_id"] for e in actual_events if e["type"] == "tool.finished"
    ]
    if started != finished:
        raise RuntimeError("TOOL_PAIRING_MISMATCH")
    aliases = {
        "month": "month",
        "period": "month",
        "day": "day",
        "flight_date": "day",
        "carrier_code": "carrier_code",
        "record_count": "record_count",
        "total_flights": "record_count",
        "total_records": "record_count",
        "cancelled_count": "cancelled_count",
        "cancelled_flights": "cancelled_count",
        "cancelled_records": "cancelled_count",
        "cancellation_rate": "cancellation_rate",
        "cancel_pct": "cancellation_rate",
        "cancel_rate": "cancellation_rate",
        "arrival_delay_null": "missing_arrival_delay",
        "arr_delay_null": "missing_arrival_delay",
        "days_with_data": "active_days",
        "min_day": "min_date",
        "max_day": "max_date",
    }
    checked, unverified, complete_grains, formal_rates = [], [], set(), 0
    for ident, artifact in case["artifacts"].items():
        meta = artifact["artifact"]
        if meta.get("date_range") != selected or meta["truncated"]:
            raise RuntimeError("ARTIFACT_RANGE_OR_TRUNCATION")
        names = [column["name"] for column in meta["columns"]]
        mapped = [aliases.get(name, name) for name in names]
        if "cancellation_rate" in mapped:
            if meta.get("query_purpose") == "reviewed_metric" and meta.get("metric_revision_ids"):
                formal_rates += 1
            elif not (
                meta.get("query_purpose") == "exploratory"
                and not meta.get("metric_revision_ids")
                and meta.get("exploration_scope")
            ):
                raise RuntimeError("FORMAL_METRIC_BINDING_REQUIRED")
        grain = [key for key in ("month", "day", "carrier_code") if key in mapped]
        expected = groups(reference, selected, grain)
        seen_keys = set()
        for index, row in enumerate(artifact["rows"]):
            decoded = [
                value.get("value") if isinstance(value, dict) and "value" in value else value
                for value in row
            ]
            key = tuple(
                str(decoded[mapped.index(name)])[:7]
                if name == "month"
                else str(decoded[mapped.index(name)])
                for name in grain
            )
            if "month" in grain:
                position = grain.index("month")
                raw_month = key[position]
                if raw_month.isdigit():
                    candidates = {
                        parts[position]
                        for parts in expected
                        if int(parts[position][-2:]) == int(raw_month)
                    }
                    if len(candidates) != 1:
                        raise RuntimeError("AMBIGUOUS_MONTH_NUMBER")
                    key = tuple(
                        next(iter(candidates)) if i == position else value
                        for i, value in enumerate(key)
                    )
            target = expected.get(key)
            if key in seen_keys:
                raise RuntimeError("DUPLICATE_GROUP_KEY")
            seen_keys.add(key)
            for name, value in zip(mapped, decoded, strict=True):
                if name in grain:
                    continue
                if target is None or name not in target:
                    unverified.append(
                        {"artifact_id": ident, "row": index, "column": name, "value": value}
                    )
                    continue
                gold = target[name]
                if name in {"min_date", "max_date"}:
                    if value != gold:
                        raise RuntimeError(f"INDEPENDENT_DATE_MISMATCH:{ident}:{index}:{name}")
                    checked.append(
                        {
                            "artifact_id": ident,
                            "row": index,
                            "column": name,
                            "actual": value,
                            "expected": gold,
                        }
                    )
                    continue
                precision = (
                    output_precision(meta["sql"], names[mapped.index(name)])
                    if meta.get("sql")
                    else None
                )
                if gold is not None and precision is not None:
                    exact = (
                        Decimal(target["cancelled_count"]) * 100 / Decimal(target["record_count"])
                        if name == "cancellation_rate"
                        else Decimal(str(gold))
                    )
                    gold = float(
                        exact.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
                    )
                if isinstance(value, str):
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                if (
                    (gold is None) != (value is None)
                    or gold is not None
                    and not math.isclose(float(value), float(gold), rel_tol=1e-9, abs_tol=1e-7)
                ):
                    raise RuntimeError(
                        f"INDEPENDENT_VALUE_MISMATCH:{ident}:{index}:{name}:{value}:{gold}"
                    )
                checked.append(
                    {
                        "artifact_id": ident,
                        "row": index,
                        "column": name,
                        "actual": value,
                        "expected": gold,
                    }
                )
        if grain == [required_grain]:
            if seen_keys != set(expected):
                raise RuntimeError("INCOMPLETE_DATE_GROUPS")
            complete_grains.add(required_grain)
    frozen_values = 0
    old_refs = 0
    for block in report["blocks"]:
        for value in values(block):
            ref = value["value_ref"]
            artifact = case["artifacts"][ref["artifact_id"]]
            columns = [c["name"] for c in artifact["artifact"]["columns"]]
            if value["raw"] != artifact["rows"][ref["row"]][columns.index(ref["column"])]:
                raise RuntimeError("FROZEN_VALUE_MISMATCH")
            frozen_values += 1
            old_refs += artifact["artifact"]["run_id"] != run["id"]
    if require_old and not old_refs:
        raise RuntimeError("OLD_EVIDENCE_NOT_REFERENCED")
    if not formal_rates:
        raise RuntimeError("FORMAL_METRIC_BINDING_REQUIRED")
    if (
        not report["draft"]["metric_revision_ids"]
        or not checked
        or unverified
        or not complete_grains
    ):
        raise RuntimeError(
            "INDEPENDENT_CHECK_INCOMPLETE:" + json.dumps(unverified, ensure_ascii=True)[:3500]
        )
    return {
        "passed": True,
        "run_id": run["id"],
        "independent_values": checked,
        "frozen_value_refs": frozen_values,
        "old_value_refs": old_refs,
    }


def main():
    """
    作用：执行明确选择的验收步骤，不批量消耗模型额度
    输入：命令行参数
    输出：进展JSON与退出码
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["baseline", "run", "check"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003/api/v1")
    parser.add_argument("--input-dir", type=Path, default=Path("data/bts"))
    parser.add_argument("--case", choices=["monthly", "followup", "narrow"], default="monthly")
    parser.add_argument("--retry-note", default="")
    parser.add_argument("--recovery-branch", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    preparation = json.loads((directory / "preparation.json").read_text(encoding="utf-8"))
    approval = json.loads((directory / "metric-approval.json").read_text(encoding="utf-8"))
    metric_id = approval["approved_metric"]["id"]
    api = Client(args.base_url)
    try:
        if args.action == "baseline":
            target = directory / "baseline.json"
            if target.exists():
                raise RuntimeError("OUTPUT_ALREADY_EXISTS")
            result = baseline(api, preparation["snapshot_id"], args.input_dir, directory / "store")
            save(target, result)
            print(
                json.dumps(
                    {
                        "baseline_complete": True,
                        "monthly": {
                            k[0]: v
                            for k, v in groups(
                                result,
                                {"start": "2026-01-01", "end_exclusive": "2026-05-01"},
                                ["month"],
                            ).items()
                        },
                    }
                )
            )
            return
        reference = json.loads((directory / "baseline.json").read_text(encoding="utf-8"))
        case_path = directory / (args.case + ".json")
        if args.action == "run":
            if case_path.exists():
                previous = json.loads(case_path.read_text(encoding="utf-8"))
                rejected = previous.get("semantic_review", {}).get("accepted") is False
                if not args.retry_note or not (
                    previous["run"]["status"] in {"failed", "cancelled", "interrupted"}
                    or rejected
                    or args.recovery_branch
                ):
                    raise RuntimeError("OUTPUT_ALREADY_EXISTS")
                attempt = 1
                while (directory / f"{args.case}-attempt-{attempt}.json").exists():
                    attempt += 1
                save(
                    directory / f"{args.case}-attempt-{attempt}.json",
                    {
                        "retry_note": args.retry_note,
                        "original_case": previous,
                    },
                )
            if args.case == "monthly":
                inv_path = directory / "investigation.json"
                if args.recovery_branch:
                    if not args.retry_note or not inv_path.exists():
                        raise RuntimeError("EXPLICIT_RECOVERY_PARENT_REQUIRED")
                    original = json.loads(inv_path.read_text(encoding="utf-8"))
                    parent = api.request("GET", f"/investigations/{original['id']}")
                    recovery_number = 1
                    recovery_path = directory / "recovery-parent.json"
                    while recovery_path.exists():
                        recovery_number += 1
                        recovery_path = directory / f"recovery-parent-{recovery_number}.json"
                    save(recovery_path, {"investigation": parent, "reason": args.retry_note})
                    inv = api.request(
                        "POST",
                        "/investigations",
                        json={
                            "conversation_id": parent["conversation_id"],
                            "snapshot_id": parent["snapshot_id"],
                            "parent_id": parent["id"],
                            "date_range": parent["date_range"],
                            "goal": (
                                "6.E真实验收：一至四月修复后月度与承运人调查"
                                f"（恢复分支{recovery_number}）"
                            ),
                            "scope": parent["scope"]
                            + " 显式修复验收分支，父调查额度和失败记录保持。",
                        },
                    )
                    save(inv_path, inv)
                elif inv_path.exists():
                    inv = json.loads(inv_path.read_text(encoding="utf-8"))
                else:
                    conversation = api.request("POST", "/conversations")
                    inv = api.request(
                        "POST",
                        "/investigations",
                        json={
                            "conversation_id": conversation["id"],
                            "snapshot_id": preparation["snapshot_id"],
                            "goal": "6.E真实验收：一至四月项目取消率及月份/承运人差异调查",
                            "scope": (
                                "仅Reporting Carrier已核验固定数据；项目取消率按已审核定义，"
                                "其他计数为明确基础探索。无外部因果证据，不调用天气/公告。"
                            ),
                            "date_range": {"start": "2026-01-01", "end_exclusive": "2026-05-01"},
                        },
                    )
                    save(inv_path, inv)
                question = (
                    "请核对一至四月固定范围并按月比较计划记录数、取消记录数"
                    "和已审核项目 cancellation_rate。"
                    "先查阅字段与已审核口径，分母含取消和备降，不把到达延误缺失排除。"
                    "用于逐项核对的月度表请用 month、record_count、cancelled_count、"
                    "cancellation_rate（百分数）列名；可用折线图辅助。"
                    "本轮仅完成月度表，选择值得下一轮核查的月份；承运人分组留待下一轮，不在本轮展开。"
                    "不要求显著异常，不自造航空公司名称。"
                    "保存本轮发现和限制，给出简洁中文报告。不要调用外部工具，不判断天气因果或季节规律。"
                )
                if case_path.exists() and not args.recovery_branch:
                    if previous["run"]["status"] in {"failed", "cancelled", "interrupted"}:
                        question = previous["run"]["question"]
                    else:
                        question = (
                            previous.get("semantic_review", {}).get("correction_request", "")
                            + question
                        )
            else:
                inv = json.loads((directory / "investigation.json").read_text(encoding="utf-8"))
                if args.case == "followup":
                    monthly_case = json.loads(
                        (directory / "monthly.json").read_text(encoding="utf-8")
                    )
                    question = (
                        monthly_case.get("semantic_review", {}).get("followup_correction", "")
                        + "继续同一调查：先read_investigation回顾，"
                        "再read_artifact复核上一轮月度证据。"
                        "报告引用上一轮已核实的月度证据及其ValueRef，不要重算替代旧证据。"
                        "根据真实前序核查中的承运人线索，本轮只聚焦OH代码，用同一已审核项目取消率"
                        "补充该承运人四个月的分组比较；查询与表格只保留OH，每月一行。"
                        "分组核对表使用 month、carrier_code、record_count、"
                        "cancelled_count、cancellation_rate（百分数）。"
                        "同时披露各组分母，说明航线构成与天数差可能影响比较，现有证据不证明原因。"
                        "本轮不查询机场数、航线数或其他派生统计，不用重复横轴的承运人柱图冒充月度图。"
                        "保存本轮发现或修正，给出包含旧证据和新增分组结果的简洁报告，不调用外部工具。"
                    )
                else:
                    monthly = groups(reference, inv["date_range"], ["month"])
                    period = max(monthly, key=lambda k: monthly[k]["cancellation_rate"])[0]
                    first = date.fromisoformat(period + "-01")
                    end = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
                    inv = api.request(
                        "POST",
                        "/investigations",
                        json={
                            "conversation_id": inv["conversation_id"],
                            "snapshot_id": inv["snapshot_id"],
                            "parent_id": inv["id"],
                            "goal": "6.E真实验收：依据已核实月度结果缩小到取消比例最高月份",
                            "scope": (
                                "固定父调查同一数据快照，显式缩小业务日期，"
                                "不自动继承不同范围的证据；项目取消率按已审核定义。"
                            ),
                            "date_range": {
                                "start": first.isoformat(),
                                "end_exclusive": end.isoformat(),
                            },
                        },
                    )
                    save(directory / "narrow-investigation.json", inv)
                    question = (
                        "已根据独立核对的月度结果将范围缩到取消比例最高月份。请核对当前固定日期范围，"
                        "按天比较全部计划记录数、取消记录数和已审核项目取消率，找出值得后续核查的日期。"
                        "核对表使用 day、record_count、cancelled_count、"
                        "cancellation_rate（百分数），给出有分母的简洁表格或折线图。"
                        "保存发现、缺失处理与限制；分支范围已改变，不复用全四月汇总当作本月证据。"
                        "不调用外部工具，不作天气或因果判断。"
                    )
            run_case(api, inv, question, metric_id, case_path)
        else:
            case = json.loads(case_path.read_text(encoding="utf-8"))
            result = check_case(
                api,
                case,
                reference,
                args.case == "followup",
                "day" if args.case == "narrow" else "month",
            )
            save(directory / (args.case + "-check.json"), result)
            case["numeric_passed"] = True
            case["passed"] = case.get("semantic_review", {}).get("accepted") is True
            save(case_path, case)
            print(json.dumps({k: v for k, v in result.items() if k != "independent_values"}))
    finally:
        api.client.close()


if __name__ == "__main__":
    main()
