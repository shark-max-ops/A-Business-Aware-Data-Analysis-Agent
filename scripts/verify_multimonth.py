"""6.B真实原件验收：独立CSV基线对照受控SQL产物，不调用模型或外部来源。"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, new_id, utc_now
from metric_pilot.data.contracts import RunContext
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.database import create_metadata_engine
from metric_pilot.settings import Settings
from verify_bts_snapshot import RAW_COLUMNS, checksum, profile_csv


def verify(config, snapshot_id, input_dir):
    """
    作用：独立原始列解析与受控查询对照逐月统计，并检查冻结来源定位
    输入：隔离存储配置、快照ID、月度原件目录
    输出：验收结果及实际成功查询产物ID
    """
    engine = create_metadata_engine(config)
    try:
        catalog = Catalog(engine, config.storage_dir)
        snapshot = catalog.snapshot(snapshot_id)
        service = QueryService(catalog, QueryExecutor(max_rows=100, timeout_seconds=10))
        internal = list(RAW_COLUMNS.values())
        columns = [
            "count(*)",
            "CAST(min(flight_date) AS VARCHAR)",
            "CAST(max(flight_date) AS VARCHAR)",
            "sum(cancelled)",
            "sum(diverted)",
        ]
        columns += [f"count(*)-count({name})" for name in internal]
        negative_names = ["scheduled_elapsed", "actual_elapsed", "air_time"]
        columns += [f"sum(CASE WHEN {name}<0 THEN 1 ELSE 0 END)" for name in negative_names]
        months, total_rows = [], 0
        for source in snapshot.manifest["sources"]:
            period = source["period"]
            original = input_dir / (period + ".csv")
            original_hash = checksum(original)
            baseline = profile_csv(original, period)
            frozen = catalog.root / "datasets" / snapshot.id / source["file"]
            hashes_match = (
                original_hash == checksum(original) == checksum(frozen) == source["sha256"]
            )
            ctx = RunContext(
                new_id("validation"), snapshot.id, "6b-no-knowledge-used", time.monotonic() + 30
            )
            sql = (
                "SELECT "
                + ", ".join(f"{expression} AS c{i}" for i, expression in enumerate(columns))
                + " FROM v_flights WHERE source_period=:period"
            )
            result = service.execute(ctx, sql, {"period": period})
            if not result.ok:
                raise DataError("VERIFICATION_QUERY_FAILED", {"cause": result.error_code})
            page = service.read_artifact(ctx.run_id, result.artifact_ids[0])
            if page["artifact"]["truncated"]:
                raise DataError("VERIFICATION_TRUNCATED")
            expected = [
                baseline["raw_rows"],
                baseline["date_min"],
                baseline["date_max"],
                sum(c["cancelled"] for c in baseline["carriers"].values()),
                sum(c["diverted"] for c in baseline["carriers"].values()),
            ]
            expected += [baseline["null_counts"][name] for name in internal]
            expected += [baseline["negative_duration_counts"][name] for name in negative_names]
            actual = page["rows"][0]
            # 对相同原行号的不同月份分别取证，确认来源ID和记录ID没有串月。
            locator = service.execute(
                ctx,
                "SELECT record_id,source_file_id,source_period,source_row_number "
                "FROM v_flights WHERE source_period=:period AND source_row_number=2",
                {"period": period},
            )
            if not locator.ok:
                raise DataError("VERIFICATION_LOCATOR_FAILED")
            location = service.read_artifact(ctx.run_id, locator.artifact_ids[0])["rows"]
            wanted_id = hashlib.sha256(f"{source['sha256']}:2".encode()).hexdigest()
            locator_match = location == [[wanted_id, source["source_file_id"], period, 2]]
            months.append(
                {
                    "period": period,
                    "source_hash_matches": hashes_match,
                    "comparison_count": len(expected),
                    "stat_values_match": actual == expected,
                    "source_locator_matches": locator_match,
                    "artifact_ids": result.artifact_ids + locator.artifact_ids,
                    "expected": expected,
                    "actual": actual,
                }
            )
            total_rows += baseline["raw_rows"]
            print(f"{period}: {len(expected)} statistics and source locator checked", flush=True)
        return {
            "snapshot_id": snapshot.id,
            "verified_at": utc_now(),
            "months": months,
            "total_rows": total_rows,
            "catalog_rows_match": total_rows == snapshot.row_count,
            "passed": total_rows == snapshot.row_count
            and bool(months)
            and all(
                m["source_hash_matches"] and m["stat_values_match"] and m["source_locator_matches"]
                for m in months
            ),
            "scope": "真实CSV、冻结原件及受控只读SQL的数值验收；未运行模型或跨月调查。",
        }
    finally:
        engine.dispose()


def main():
    """
    作用：独占保存验收结果，只向明确指定的隔离存储写查询审计和产物
    输入：命令行存储、快照、原件及输出路径
    输出：JSON验收结果与退出码
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--input-dir", type=Path, default=Path("data/bts"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("OUTPUT_ALREADY_EXISTS")
    config = Settings(_env_file=None, storage_dir=args.storage_dir)
    result = verify(config, args.snapshot_id, args.input_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
