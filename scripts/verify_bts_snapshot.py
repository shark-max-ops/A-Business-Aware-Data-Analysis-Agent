"""用独立的原始 CSV 计算核对受限查询产物；这是人工验收脚本，不是 Agent。"""

import argparse
import csv
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, new_id, utc_now
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.settings import Settings

# 与导入器独立的原始导出列映射，防止同一解析错误同时产生答案与校验值。
RAW_COLUMNS = {
    "FL_DATE": "flight_date",
    "OP_UNIQUE_CARRIER": "carrier_code",
    "OP_CARRIER_AIRLINE_ID": "carrier_id",
    "TAIL_NUM": "tail_number",
    "OP_CARRIER_FL_NUM": "flight_number",
    "ORIGIN_AIRPORT_ID": "origin_airport_id",
    "ORIGIN": "origin_code",
    "DEST_AIRPORT_ID": "dest_airport_id",
    "DEST": "dest_code",
    "CRS_DEP_TIME": "scheduled_dep_local",
    "DEP_TIME": "actual_dep_local",
    "DEP_DELAY": "departure_delay",
    "CRS_ARR_TIME": "scheduled_arr_local",
    "ARR_TIME": "actual_arr_local",
    "ARR_DELAY": "arrival_delay",
    "ARR_DELAY_NEW": "arrival_delay_nonnegative",
    "ARR_DEL15": "arrival_delayed_15",
    "CANCELLED": "cancelled",
    "CANCELLATION_CODE": "cancellation_code",
    "DIVERTED": "diverted",
    "CRS_ELAPSED_TIME": "scheduled_elapsed",
    "ACTUAL_ELAPSED_TIME": "actual_elapsed",
    "AIR_TIME": "air_time",
    "DISTANCE": "distance_miles",
    "CARRIER_DELAY": "delay_carrier",
    "WEATHER_DELAY": "delay_weather",
    "NAS_DELAY": "delay_nas",
    "SECURITY_DELAY": "delay_security",
    "LATE_AIRCRAFT_DELAY": "delay_late_aircraft",
}
CAUSE_COLUMNS = [
    "CARRIER_DELAY",
    "WEATHER_DELAY",
    "NAS_DELAY",
    "SECURITY_DELAY",
    "LATE_AIRCRAFT_DELAY",
]


def checksum(path):
    """
    作用：独立计算原始文件哈希
    输入：文件路径
    输出：SHA-256
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def totals():
    """
    作用：建立独立的精确统计累加器
    输入：无
    输出：计数与 Decimal 求和容器
    """
    return {
        "scheduled": 0,
        "cancelled": 0,
        "diverted": 0,
        "eligible": 0,
        "delayed": 0,
        "signed_sum": Decimal(0),
        "nonnegative_sum": Decimal(0),
    }


def profile_csv(path, period):
    """
    作用：只用 csv、datetime 与 Decimal 直接计算输入基线
    输入：BTS 数据库导出 CSV 和月份
    输出：原始行数、日期、质量与逐航司基线
    """
    raw_rows, nulls, carriers, dates, months = (
        0,
        Counter(),
        defaultdict(totals),
        Counter(),
        Counter(),
    )
    negative_durations = Counter(
        {name: 0 for name in ("air_time", "scheduled_elapsed", "actual_elapsed")}
    )
    cause_sum, cause_seen = Counter(), Counter()
    signed_negative, threshold_mismatch, nonnegative_mismatch, overlap, missing_any_cause = (
        0,
        0,
        0,
        0,
        0,
    )
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(RAW_COLUMNS).issubset(reader.fieldnames or []):
            raise ValueError("RAW_EXPORT_COLUMNS_REQUIRED")
        for row in reader:
            raw_rows += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError("INVALID_RAW_ROW")
            for raw, internal in RAW_COLUMNS.items():
                nulls[internal] += not row[raw].strip()
            when = datetime.strptime(row["FL_DATE"], "%m/%d/%Y %I:%M:%S %p")
            if when.hour or when.minute or when.second:
                raise ValueError("NONMIDNIGHT_BUSINESS_DATE")
            months[when.strftime("%Y-%m")] += 1
            for raw in ("CRS_ELAPSED_TIME", "ACTUAL_ELAPSED_TIME", "AIR_TIME"):
                if row[raw] and Decimal(row[raw]) < 0:
                    negative_durations[RAW_COLUMNS[raw]] += 1
            signed = Decimal(row["ARR_DELAY"]) if row["ARR_DELAY"] else None
            nonnegative = Decimal(row["ARR_DELAY_NEW"]) if row["ARR_DELAY_NEW"] else None
            flag = Decimal(row["ARR_DEL15"]) if row["ARR_DEL15"] else None
            if signed is not None and flag is not None:
                threshold_mismatch += int(signed >= 15) != flag
            if signed is not None and nonnegative is not None:
                nonnegative_mismatch += max(Decimal(0), signed) != nonnegative
            cancelled, diverted = int(Decimal(row["CANCELLED"])), int(Decimal(row["DIVERTED"]))
            overlap += cancelled == diverted == 1
            if when.strftime("%Y-%m") != period:
                continue
            dates[when.date().isoformat()] += 1
            signed_negative += signed is not None and signed < 0
            carrier = carriers[int(row["OP_CARRIER_AIRLINE_ID"])]
            carrier["scheduled"] += 1
            carrier["cancelled"] += cancelled
            carrier["diverted"] += diverted
            if (
                cancelled == diverted == 0
                and signed is not None
                and nonnegative is not None
                and flag in (0, 1)
            ):
                carrier["eligible"] += 1
                carrier["delayed"] += int(flag)
                carrier["signed_sum"] += signed
                carrier["nonnegative_sum"] += nonnegative
            missing_any_cause += any(not row[name] for name in CAUSE_COLUMNS)
            for name in CAUSE_COLUMNS:
                if row[name]:
                    cause_sum[name] += Decimal(row[name])
                    cause_seen[name] += 1
    return {
        "raw_rows": raw_rows,
        "exposed_rows": sum(dates.values()),
        "months": dict(months),
        "date_min": min(dates),
        "date_max": max(dates),
        "days": len(dates),
        "null_counts": dict(nulls),
        "negative_duration_counts": dict(negative_durations),
        "threshold_mismatches": threshold_mismatch,
        "nonnegative_mapping_mismatches": nonnegative_mismatch,
        "cancelled_diverted_overlap": overlap,
        "negative_arrivals": signed_negative,
        "missing_any_cause": missing_any_cause,
        "carriers": dict(carriers),
        "cause_sum": {
            name: cause_sum[name] if cause_seen[name] else None for name in CAUSE_COLUMNS
        },
    }


def unpack(value):
    """
    作用：解释冻结结果的类型标签
    输入：产物单元格
    输出：可比较的精确值或日期字符串
    """
    if isinstance(value, dict):
        if value["type"] == "integer":
            return int(value["value"])
        if value["type"] == "decimal":
            return Decimal(value["value"])
        return value["value"]
    return value


def check_equal(actual, expected, label):
    """
    作用：比较整数、空值和带容差的浮点计算
    输入：实际值、独立基线与检查标签
    输出：无；不一致时失败
    """
    if expected is None or isinstance(expected, (str, int)):
        equal = actual == expected
    else:
        equal = actual is not None and math.isclose(
            float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-8
        )
    if not equal:
        raise ValueError(f"CHECK_FAILED:{label}")


def verify(input_path, snapshot_id, settings):
    """
    作用：比较独立 CSV 基线和受限查询，保存真实证据引用
    输入：源文件、固定快照和应用配置
    输出：只有全部检查通过才产生的验收记录
    """
    began = time.monotonic()
    engine = create_metadata_engine(settings)
    try:
        catalog = Catalog(engine, settings.storage_dir)
        snapshot = catalog.snapshot(snapshot_id)
        if checksum(input_path) != snapshot.file_hash:
            raise ValueError("SOURCE_HASH_MISMATCH")
        baseline = profile_csv(input_path, snapshot.period)
        if checksum(input_path) != snapshot.file_hash:
            raise ValueError("SOURCE_CHANGED_DURING_VERIFICATION")
        quality = snapshot.quality_report
        for key, expected in [
            ("rows_raw", baseline["raw_rows"]),
            ("rows_exposed", baseline["exposed_rows"]),
            ("null_counts", baseline["null_counts"]),
            ("negative_duration_counts", baseline["negative_duration_counts"]),
            ("arrival_threshold_mismatches", baseline["threshold_mismatches"]),
            ("cancelled_diverted_overlap", baseline["cancelled_diverted_overlap"]),
        ]:
            if quality[key] != expected:
                raise ValueError(f"QUALITY_MISMATCH:{key}")
        if baseline["nonnegative_mapping_mismatches"]:
            raise ValueError("ARR_DELAY_NEW_MAPPING_REQUIRES_REVIEW")
        ctx = RunContext(
            new_id("run"),
            snapshot_id,
            "knowledge_empty_v1",
            time.monotonic() + settings.run_timeout_seconds,
        )
        service = QueryService(
            catalog, QueryExecutor(settings.sql_timeout_seconds, settings.sql_max_rows)
        )
        artifacts = {}

        def query(name, sql):
            """
            作用：仅通过正式受限服务执行验收 SQL 并读取冻结值
            输入：检查名与人工审核 SQL
            输出：解码结果行
            """
            result = service.execute(ctx, sql, {})
            if not result.ok:
                raise ValueError(f"QUERY_FAILED:{name}:{result.error_code}")
            artifact_id = result.artifact_ids[0]
            saved = service.read_artifact(ctx.run_id, artifact_id, limit=100)
            if saved["artifact"]["truncated"] or saved["next_offset"] is not None:
                raise ValueError(f"INCOMPLETE_VERIFICATION_RESULT:{name}")
            artifacts[name] = artifact_id
            return [[unpack(value) for value in row] for row in saved["rows"]]

        coverage = query(
            "coverage",
            """SELECT COUNT(*) AS records, MIN(flight_date) AS first_date,
            MAX(flight_date) AS last_date, COUNT(DISTINCT flight_date) AS days,
            SUM(CASE WHEN arrival_delay<0 THEN 1 ELSE 0 END) AS negative_arrivals FROM v_flights""",
        )
        expected = [
            baseline[k]
            for k in ["exposed_rows", "date_min", "date_max", "days", "negative_arrivals"]
        ]
        for actual, expected_value in zip(coverage[0], expected, strict=True):
            check_equal(actual, expected_value, "coverage")
        population = query(
            "population",
            """SELECT carrier_id, COUNT(*) AS scheduled,
            SUM(cancelled) AS cancelled, SUM(diverted) AS diverted,
            100.0*SUM(cancelled)/NULLIF(COUNT(*),0) AS cancellation_rate_pct,
            100.0*SUM(diverted)/NULLIF(COUNT(*),0) AS diversion_rate_pct
            FROM v_flights GROUP BY carrier_id ORDER BY carrier_id""",
        )
        if {row[0] for row in population} != set(baseline["carriers"]):
            raise ValueError("CARRIER_POPULATION_MISMATCH")
        for carrier_id, *values in population:
            item = baseline["carriers"][carrier_id]
            expected = [item[k] for k in ["scheduled", "cancelled", "diverted"]] + [
                100 * item["cancelled"] / item["scheduled"],
                100 * item["diverted"] / item["scheduled"],
            ]
            for actual, expected_value in zip(values, expected, strict=True):
                check_equal(actual, expected_value, "population")
        arrivals = query(
            "eligible_arrivals",
            """SELECT carrier_id, COUNT(*) AS eligible,
            SUM(arrival_delayed_15) AS delayed, AVG(arrival_delay) AS mean_arrival_deviation,
            AVG(arrival_delay_nonnegative) AS mean_nonnegative_delay,
            100.0*SUM(arrival_delayed_15)/NULLIF(COUNT(*),0) AS delay_rate_pct
            FROM v_flights WHERE cancelled=0 AND diverted=0 AND arrival_delay IS NOT NULL
            AND arrival_delay_nonnegative IS NOT NULL AND arrival_delayed_15 IN (0,1)
            GROUP BY carrier_id ORDER BY carrier_id""",
        )
        if {row[0] for row in arrivals} != {
            k for k, v in baseline["carriers"].items() if v["eligible"]
        }:
            raise ValueError("ELIGIBLE_CARRIER_MISMATCH")
        for carrier_id, *values in arrivals:
            item = baseline["carriers"][carrier_id]
            expected = [
                item["eligible"],
                item["delayed"],
                item["signed_sum"] / item["eligible"],
                item["nonnegative_sum"] / item["eligible"],
                100 * item["delayed"] / item["eligible"],
            ]
            for actual, expected_value in zip(values, expected, strict=True):
                check_equal(actual, expected_value, "eligible_arrivals")
        causes = query(
            "reported_causes",
            """SELECT SUM(delay_carrier), SUM(delay_weather),
            SUM(delay_nas), SUM(delay_security), SUM(delay_late_aircraft),
            SUM(CASE WHEN delay_carrier IS NULL OR delay_weather IS NULL OR delay_nas IS NULL
                OR delay_security IS NULL OR delay_late_aircraft IS NULL THEN 1 ELSE 0 END)
            FROM v_flights""",
        )
        expected = [baseline["cause_sum"][name] for name in CAUSE_COLUMNS] + [
            baseline["missing_any_cause"]
        ]
        for actual, expected_value in zip(causes[0], expected, strict=True):
            check_equal(actual, expected_value, "reported_causes")
        return {
            "kind": "manual_csv_crosscheck",
            "all_checks_passed": True,
            "created_at": utc_now(),
            "snapshot_id": snapshot_id,
            "source_sha256": snapshot.file_hash,
            "run_id": ctx.run_id,
            "artifact_ids": artifacts,
            "independent_baseline": baseline,
            "duration_ms": int((time.monotonic() - began) * 1000),
            "scope": "项目有效到达口径；仅核对源 CSV 与执行结果，不证明官方文件完整性或因果关系",
        }
    finally:
        engine.dispose()


def main():
    """
    作用：运行人工验收并在忽略的 storage 中保存验收记录
    输入：命令行文件与固定快照 ID
    输出：ASCII JSON 状态与记录
    """
    parser = argparse.ArgumentParser(description="独立核对 BTS 导出 CSV 与快照查询")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    args = parser.parse_args()
    settings = Settings()
    try:
        result = verify(args.input, args.snapshot_id, settings)
        folder = settings.storage_dir / "validation"
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"{args.snapshot_id}.json"
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=True, default=str))
    except (DataError, ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "all_checks_passed": False,
                    "error": exc.code
                    if isinstance(exc, DataError)
                    else type(exc).__name__ + ":" + str(exc)
                    if isinstance(exc, ValueError)
                    else "FILE_UNAVAILABLE",
                },
                ensure_ascii=True,
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
