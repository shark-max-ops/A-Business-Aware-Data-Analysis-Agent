"""阶段7真实验收：保留旧失败、隔离存储、独立CSV及真实模型路径。"""

import argparse
import json
import math
import shutil
import sqlite3
from pathlib import Path

from metric_pilot.data.common import DataError, utc_now
from metric_pilot.reports.materialize import content_hash, materialize
from verify_agent import Client
from verify_multimonth_investigation import (
    baseline,
    checksum,
    groups,
    output_precision,
    run_case,
    save,
)

MAIN = {"start": "2026-01-01", "end_exclusive": "2026-05-01"}
GUIDE = (
    "使用已审核项目cancellation_rate，分母含取消与备降；不调用外部工具，不作因果判断。"
    "查询列使用month（YYYY-MM）或day（YYYY-MM-DD）、carrier_code（如需）、"
    "record_count、cancelled_count、cancellation_rate（0至100）。"
    "输出完整结果表，声明display_scope的可见dimensions和必要population；"
    "主date_range须为服务端固定范围。跨月图表要有唯一月份与航司组合分类，否则用表格。"
    "文字中分组身份与数量均通过ValueRef并声明row_bindings；合计或差值须先有成功产物。"
    "报告text只写无数量的限制与下一步，数量放在表内；不能手写万条、近似值或范围。"
    "没有明确样本判定标准时，不宣称样本量足够或差异显著。"
    "保存一条或少量带依据的发现，再生成简洁中文报告；说明样本量、缺失、比较限制。"
)
QUESTIONS = {
    "monthly": "按月比较全部计划记录数、取消记录数与项目取消率，选择后续核查线索。本轮只查月度。",
    "complex": (
        "继续同调查：回顾已核实月度结果，按月份×Reporting Carrier完整比较所有航司。"
        "需要完整航司月份分组表，不只查几家；每行展示月份、航司、分母、取消数和取消率。"
        "选择实际值得核查的组别，用同行绑定解释，不能把其他行当作OH、OO或YX。"
        "发现只记录一个真实行：月份、航司、分母、取消数与取消率全部占位引用同一行，"
        "不要跨行比较或复述组数与缺失计数；完整复杂比较由全量分组表呈现。"
    ),
    "followup": (
        "继续同调查：read_investigation回顾，读取并引用已完成旧轮月度产物，保留旧证据ValueRef。"
        "本轮只聚焦OH，查询与表格只保留OH，按月展示carrier_code、分母、取消数和取消率。"
        "旧月度证据与新OH结果分别展示，不把全航司表标题改为OH。"
    ),
    "narrow": (
        "按当前固定月份逐日比较全部计划记录数、取消数与项目取消率，"
        "完整覆盖日粒度，选择后续核查日期。"
    ),
}


ALIASES = {
    "cancelled_null_count": "cancelled_null",
    "cancelled_null_rows": "cancelled_null",
    "day_count": "active_days",
    "total_records": "record_count",
    "total_cancelled": "cancelled_count",
    "overall_rate": "cancellation_rate",
}


def prepare(directory, source):
    """
    作用：复制隔离存储并保留来源指纹，不重新批准指标
    输入：新目录与既有6.E验收目录
    输出：无
    """
    directory.mkdir(parents=True, exist_ok=False)
    old = source / "store"
    protected = {str(p): checksum(p) for p in old.rglob("*") if p.is_file()}
    shutil.copytree(old, directory / "store", ignore=shutil.ignore_patterns("metadata.sqlite*"))
    with sqlite3.connect(f"file:{old / 'metadata.sqlite'}?mode=ro", uri=True) as origin:
        with sqlite3.connect(directory / "store/metadata.sqlite") as target:
            origin.backup(target)
    preparation = json.loads((source / "preparation.json").read_text(encoding="utf-8"))
    approval = json.loads((source / "metric-approval.json").read_text(encoding="utf-8"))
    save(
        directory / "preparation.json",
        {
            "created_at": utc_now(),
            "snapshot_id": preparation["snapshot_id"],
            "source_directory": str(source),
            "protected_source_files": protected,
            "approval_reused_without_changes": approval,
            "context_tokens": 48000,
            "not_project_default": True,
        },
    )


def start_case(api, directory, name, retry_note=""):
    """
    作用：执行显式选择的真实模型案例，禁止覆盖既有尝试
    输入：正式API、目录与案例名
    输出：无
    """
    target = directory / f"{name}.json"
    if target.exists():
        previous = json.loads(target.read_text(encoding="utf-8"))
        if not retry_note or not (
            previous["run"]["status"] in {"failed", "cancelled", "interrupted"}
            or previous.get("semantic_review", {}).get("accepted") is False
        ):
            raise RuntimeError("EXPLICIT_REJECTED_RETRY_REQUIRED")
        attempt = 1
        while (directory / f"{name}-attempt-{attempt}.json").exists():
            attempt += 1
        save(
            directory / f"{name}-attempt-{attempt}.json",
            {"retry_note": retry_note, "original_case": previous},
        )
    preparation = json.loads((directory / "preparation.json").read_text(encoding="utf-8"))
    if name == "monthly" and not (retry_note and previous["run"]["status"] == "completed"):
        conversation = api.request("POST", "/conversations")
        inv = api.request(
            "POST",
            "/investigations",
            json={
                "conversation_id": conversation["id"],
                "snapshot_id": preparation["snapshot_id"],
                "date_range": MAIN,
                "goal": "阶段7：真实月度与复杂航司分组解释验收",
                "scope": "Reporting Carrier固定一至四月；仅已审核项目取消率，禁止外部因果推断。",
            },
        )
        save(directory / "investigation.json", inv)
    else:
        inv = json.loads((directory / "investigation.json").read_text(encoding="utf-8"))
        if name == "narrow":
            reference = json.loads((directory / "baseline.json").read_text(encoding="utf-8"))
            monthly = groups(reference, MAIN, ["month"])
            month = max(monthly, key=lambda key: monthly[key]["cancellation_rate"])[0]
            year, number = map(int, month.split("-"))
            end = f"{year + (number == 12):04d}-{number % 12 + 1:02d}-01"
            inv = api.request(
                "POST",
                "/investigations",
                json={
                    "conversation_id": inv["conversation_id"],
                    "snapshot_id": inv["snapshot_id"],
                    "parent_id": inv["id"],
                    "date_range": {"start": month + "-01", "end_exclusive": end},
                    "goal": "阶段7：根据独立月度结果缩小范围的日粒度分支",
                    "scope": "显式缩小月份，不复用其他范围汇总；不调用外部来源。",
                },
            )
            save(directory / "narrow-investigation.json", inv)
    metric = preparation["approval_reused_without_changes"]["approved_metric"]["id"]
    correction = (
        previous.get("semantic_review", {}).get("correction_request", "") if retry_note else ""
    )
    run_case(api, inv, correction + QUESTIONS[name] + GUIDE, metric, target)


def check_case(directory, name):
    """
    作用：对照原CSV分组逐值核对报告，不自动宣布语义通过
    输入：独立基线及真实案例文件
    输出：检查摘要
    """
    case = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    reference = json.loads((directory / "baseline.json").read_text(encoding="utf-8"))
    if case["run"]["status"] != "completed" or "report" not in case or not case["findings"]:
        raise RuntimeError("COMPLETED_REPORT_AND_FINDINGS_REQUIRED")
    run, report = case["run"], case["report"]
    if report["date_range"] != run["date_range"]:
        raise RuntimeError("MAIN_RANGE_CHANGED")
    required = (
        ["day"]
        if name == "narrow"
        else ["month", "carrier_code"]
        if name in {"complex", "followup"}
        else ["month"]
    )
    carrier = "OH" if name == "followup" else None
    expected_keys = set(groups(reference, run["date_range"], required, carrier))
    checked, primary, old_refs = [], False, 0
    for ident, item in case["artifacts"].items():
        meta, rows = item["artifact"], item["rows"]
        if meta["date_range"] != run["date_range"] or meta["truncated"]:
            raise RuntimeError("ARTIFACT_RANGE_OR_TRUNCATION")
        columns = [c["name"] for c in meta["columns"]]
        grain = [c for c in ("month", "day", "carrier_code") if c in columns]
        expected = groups(reference, run["date_range"], grain)
        keys = set()
        for index, raw in enumerate(rows):
            row = {
                key: value.get("value") if isinstance(value, dict) else value
                for key, value in zip(columns, raw, strict=True)
            }
            # DATE_TRUNC可返回月首DATE；只规范合法月首，不截断任意字符串。
            if "month" in row and len(str(row["month"])) == 10:
                from datetime import date

                day = date.fromisoformat(row["month"])
                if day.day != 1:
                    raise RuntimeError("INVALID_MONTH_START")
                row["month"] = day.isoformat()[:7]
            key = tuple(str(row[c]) for c in grain)
            if key in keys or key not in expected:
                raise RuntimeError("GROUP_IDENTITY_MISMATCH")
            keys.add(key)
            for column, value in row.items():
                if column in grain:
                    continue
                lookup = ALIASES.get(column, column)
                if column == "zero_or_null_days":
                    # 基线每组均来自实际记录；缺日另由完整日期集合核对，不补零造日组。
                    expected[key][column] = 0
                if lookup not in expected[key]:
                    raise RuntimeError(f"UNVERIFIED_COLUMN:{column}")
                gold = expected[key][lookup]
                precision = output_precision(meta["sql"], column)
                if precision is not None and gold is not None:
                    from decimal import ROUND_HALF_UP, Decimal

                    exact = (
                        (
                            Decimal(expected[key]["cancelled_count"])
                            * 100
                            / Decimal(expected[key]["record_count"])
                        )
                        if lookup == "cancellation_rate"
                        else Decimal(str(gold))
                    )
                    gold = float(
                        exact.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
                    )
                if isinstance(gold, str):
                    match = gold == value
                else:
                    match = (
                        gold is None
                        and value is None
                        or gold is not None
                        and value is not None
                        and math.isclose(float(value), float(gold), rel_tol=1e-9, abs_tol=1e-7)
                    )
                if not match:
                    raise RuntimeError(f"INDEPENDENT_VALUE_MISMATCH:{ident}:{index}:{column}")
                checked.append(
                    {
                        "artifact_id": ident,
                        "row": index,
                        "column": column,
                        "actual": value,
                        "expected": gold,
                        "group": key,
                    }
                )
        if grain == required and keys == expected_keys:
            primary = True
        if any(ALIASES.get(c, c) == "cancellation_rate" for c in columns) and (
            meta["query_purpose"] != "reviewed_metric" or not meta["metric_revision_ids"]
        ):
            raise RuntimeError("FORMAL_METRIC_BINDING_REQUIRED")
        old_refs += meta["run_id"] != run["id"]
    if not checked or not primary or name == "followup" and not old_refs:
        raise RuntimeError("INDEPENDENT_CHECK_INCOMPLETE")
    result = {
        "numeric_passed": True,
        "semantic_accepted": False,
        "checked_values": checked,
        "old_artifacts": old_refs,
        "run_id": run["id"],
    }
    save(directory / f"{name}-check.json", result)
    return {"numeric_passed": True, "values": len(checked), "old_artifacts": old_refs}


def regressions(directory, source):
    """
    作用：分类真实拒绝案例、离线复验确定性防护并核对旧报告不可变
    输入：新验收目录与只读原6.E目录
    输出：分类及原报告哈希检查摘要
    """
    categories = {
        "carrier_identity": "monthly-attempt-10.json",
        "display_population": "followup-attempt-2.json",
        "missing_time_dimension": "followup-attempt-2.json",
        "date_range": "monthly-attempt-2.json",
        "unfrozen_total": "monthly-attempt-4.json",
    }
    results = []
    with sqlite3.connect(directory / "store/metadata.sqlite") as clone:
        for category, name in categories.items():
            path = source / name
            original = json.loads(path.read_text(encoding="utf-8"))
            case = original.get("original_case", original)
            if case["semantic_review"]["accepted"] is not False:
                raise RuntimeError("REJECTED_SOURCE_REQUIRED")
            report = case["report"]
            row = clone.execute(
                "SELECT payload FROM report_revisions WHERE id=?", (report["revision_id"],)
            ).fetchone()
            payload = json.loads(row[0])
            error = None
            try:
                materialize(payload["draft"], payload)
            except DataError as exc:
                error = exc.code
            results.append(
                {
                    "category": category,
                    "source": str(path),
                    "source_sha256": checksum(path),
                    "run_id": case["run"]["id"],
                    "revision_id": report["revision_id"],
                    "original_semantic_rejection_retained": True,
                    "current_replay_error": error,
                    "boundary": "Legacy free text requires semantic review; "
                    "explicit declarations have separate synthetic rejection tests.",
                }
            )
        if not any(r["current_replay_error"] == "REPORT_CHART_AXIS_AMBIGUOUS" for r in results):
            raise RuntimeError("REAL_DUPLICATE_AXIS_NOT_REJECTED")
        with sqlite3.connect(f"file:{source / 'store/metadata.sqlite'}?mode=ro", uri=True) as old:
            protected = old.execute(
                "SELECT id,payload,content_hash FROM report_revisions"
            ).fetchall()
        for ident, raw, digest in protected:
            now = clone.execute(
                "SELECT payload,content_hash FROM report_revisions WHERE id=?", (ident,)
            ).fetchone()
            if now != (raw, digest) or content_hash(json.loads(raw)) != digest:
                raise RuntimeError("OLD_REPORT_CHANGED")
    result = {
        "categories": results,
        "unchanged_report_revisions": len(protected),
        "created_at": utc_now(),
        "real_model_rerun": False,
    }
    save(directory / "rejected-baseline.json", result)
    return {"classified_categories": len(results), "unchanged_reports": len(protected)}


def main():
    """
    作用：按显式动作运行验收，不自动重试付费模型
    输入：命令行参数
    输出：实际检查摘要
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "baseline", "run", "check", "protect", "regressions"]
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--source", type=Path, default=Path("storage/validation/bts-6e/acceptance-20261109")
    )
    parser.add_argument("--case", choices=list(QUESTIONS), default="monthly")
    parser.add_argument("--base-url", default="http://127.0.0.1:8007/api/v1")
    parser.add_argument("--retry-note", default="")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.directory, args.source)
        print("prepared", flush=True)
        return
    preparation = json.loads((args.directory / "preparation.json").read_text(encoding="utf-8"))
    if args.action == "regressions":
        print(json.dumps(regressions(args.directory, args.source)), flush=True)
        return
    if args.action == "check":
        print(json.dumps(check_case(args.directory, args.case)), flush=True)
        return
    if args.action == "protect":
        checks = {
            path: checksum(Path(path)) == value
            for path, value in preparation["protected_source_files"].items()
            if not path.endswith("metadata.sqlite-shm")
        }
        # SQLite只读WAL访问会改变共享内存读标记；业务数据库及WAL仍逐字节保护。
        target = args.directory / "source-immutability.json"
        if target.exists() and not (args.directory / "source-immutability-initial.json").exists():
            shutil.copy2(target, args.directory / "source-immutability-initial.json")
        save(
            target,
            {
                "stable_files": checks,
                "excluded_transient_files": [
                    path
                    for path in preparation["protected_source_files"]
                    if path.endswith("metadata.sqlite-shm")
                ],
                "reason": "SQLite shared-memory reader marks are transient; "
                "main database and WAL are protected.",
            },
        )
        if not all(checks.values()):
            raise RuntimeError("SOURCE_CHANGED")
        print(json.dumps({"unchanged_files": len(checks)}), flush=True)
        return
    api = Client(args.base_url)
    try:
        if args.action == "baseline":
            target = args.directory / "baseline.json"
            if target.exists():
                raise RuntimeError("OUTPUT_ALREADY_EXISTS")
            save(
                target,
                baseline(
                    api, preparation["snapshot_id"], Path("data/bts"), args.directory / "store"
                ),
            )
        else:
            start_case(api, args.directory, args.case, args.retry_note)
    finally:
        api.client.close()


if __name__ == "__main__":
    main()
