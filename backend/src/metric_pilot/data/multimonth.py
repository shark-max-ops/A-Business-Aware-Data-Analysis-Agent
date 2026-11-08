"""阶段6.B：使用月度文件集合建立不可变多月快照，不激活或启动调查。"""

import argparse
import csv
import json
import math
import shutil
import signal
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import duckdb
from sqlalchemy import select

from metric_pilot.data.audit import month_days
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, canonical_bytes, digest, new_id, utc_now
from metric_pilot.data.ingest import (
    DESCRIPTIONS,
    DURATION_COLUMNS,
    FIELDS,
    importer_version,
    prepare_month,
)
from metric_pilot.data.models import DataSnapshot
from metric_pilot.data.serialization import encode_cell
from metric_pilot.database import create_metadata_engine, inspect_database
from metric_pilot.settings import Settings


class ImportControl:
    """
    作用：组合导入取消与实际截止时间，终止正在执行的DuckDB语句
    输入：取消事件和显式导入时间预算
    输出：导入控制对象
    """

    def __init__(self, cancel, timeout_seconds):
        """
        作用：校验预算并固定单调时钟截止时间
        输入：cancel - 可选事件；timeout_seconds - 秒数
        输出：无
        """
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise DataError("INVALID_IMPORT_TIMEOUT")
        self.cancel = cancel
        self.deadline = time.monotonic() + timeout_seconds

    def check(self):
        """
        作用：在文件处理及发布边界检查取消和截止时间
        输入：无
        输出：无，条件触发时抛出安全错误
        """
        if self.cancel is not None and self.cancel.is_set():
            raise DataError("IMPORT_CANCELLED")
        if time.monotonic() >= self.deadline:
            raise DataError("IMPORT_TIMEOUT")

    @contextmanager
    def watch(self, connection):
        """
        作用：在当前连接执行期间监测取消，调用实际数据库中断
        输入：connection - 本次导入连接
        输出：上下文，退出时等待监测线程结束
        """
        stop = threading.Event()

        def monitor():
            """
            作用：只对本次连接发送取消信号
            输入：无
            输出：无
            """
            while not stop.wait(0.025):
                try:
                    self.check()
                except DataError:
                    connection.interrupt()
                    return

        thread = threading.Thread(target=monitor, daemon=True)
        self.check()
        thread.start()
        try:
            yield
            self.check()
        except duckdb.Error:
            self.check()
            raise
        finally:
            stop.set()
            thread.join()


def validate_inputs(input_dir, periods, audit_manifest, synthetic):
    """
    作用：检查月份集合与6.A清单，不把清单中的路径当作访问权限
    输入：固定输入目录、月份、可选清单与合成声明
    输出：有序月份和按月份索引的审计记录、原审计字节
    """
    if len(periods) < 2 or len(set(periods)) != len(periods):
        raise DataError("UNIQUE_MULTIPLE_PERIODS_REQUIRED")
    try:
        for period in periods:
            month_days(period)
    except ValueError:
        raise DataError("INVALID_PERIOD") from None
    periods = sorted(periods)
    for left, right in zip(periods, periods[1:]):
        first = date.fromisoformat(left + "-01")
        if date(first.year + (first.month == 12), first.month % 12 + 1, 1).isoformat()[:7] != right:
            raise DataError("MISSING_MONTH")
    paths = [(input_dir / (period + ".csv")).resolve() for period in periods]
    if any(not p.is_file() or not p.is_relative_to(input_dir.resolve()) for p in paths):
        raise DataError("MONTH_FILE_UNAVAILABLE")
    if len(set(paths)) != len(paths):
        raise DataError("DUPLICATE_SOURCE_FILE")
    if audit_manifest is None:
        if not synthetic:
            raise DataError("AUDIT_MANIFEST_REQUIRED")
        return periods, {}, None
    try:
        audit_bytes = audit_manifest.read_bytes()
        audit = json.loads(audit_bytes)
        if audit["stage"] != "6.A" or audit["technical_checks_passed"] is not True:
            raise ValueError()
        entries = {m["period"]: m for m in audit["months"]}
        if len(entries) != len(audit["months"]):
            raise ValueError()
        for period in periods:
            m = entries[period]
            if m["technical_checks_passed"] is not True or m["scan_complete"] is not True:
                raise ValueError()
            if m["sha256_before"] != m["sha256_after"] or m["technical_blockers"]:
                raise ValueError()
        if len({entries[p]["sha256_before"] for p in periods}) != len(periods):
            raise DataError("DUPLICATE_SOURCE_FILE")
    except DataError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise DataError("INVALID_AUDIT_MANIFEST") from None
    return periods, entries, audit_bytes


def verify_month(connection, period, quality):
    """
    作用：核对实际入库的逐月行数、日期、空值、取消/备降与负时长
    输入：导入连接、月份及从冻结CSV重算的质量结果
    输出：实际日期及标记统计，差异拒绝发布
    """
    row = connection.execute(
        "SELECT count(*), min(flight_date), max(flight_date), "
        "count(*) FILTER (WHERE cancelled=1), count(*) FILTER (WHERE diverted=1) "
        "FROM raw_flights WHERE source_period=?",
        [period],
    ).fetchone()
    if (
        row[0] != quality["rows_raw"]
        or not row[1]
        or row[1].isoformat()[:7] != period
        or row[2].isoformat()[:7] != period
    ):
        raise DataError("LOADED_MONTH_MISMATCH")
    for _, name, *_ in FIELDS:
        count = connection.execute(
            f'SELECT count(*) FROM raw_flights WHERE source_period=? AND "{name}" IS NULL',
            [period],
        ).fetchone()[0]
        if count != quality["null_counts"][name]:
            raise DataError("LOADED_NULL_COUNT_MISMATCH")
    for name in DURATION_COLUMNS:
        count = connection.execute(
            f'SELECT count(*) FROM raw_flights WHERE source_period=? AND "{name}" < 0',
            [period],
        ).fetchone()[0]
        if count != quality["negative_duration_counts"][name]:
            raise DataError("LOADED_QUALITY_MISMATCH")
    daily = connection.execute(
        "SELECT flight_date, count(*) FROM raw_flights WHERE source_period=? "
        "GROUP BY flight_date ORDER BY flight_date",
        [period],
    ).fetchall()
    return {
        "date_min": row[1].isoformat(),
        "date_max": row[2].isoformat(),
        "cancelled": row[3],
        "diverted": row[4],
        "daily_counts": {day.isoformat(): count for day, count in daily},
    }


def ingest_months(
    settings,
    input_dir,
    periods,
    *,
    audit_manifest=None,
    synthetic=False,
    cancel=None,
    timeout_seconds=600,
):
    """
    作用：独占准备、检查并登记一个新多月快照，默认版本始终不变
    输入：配置、月度文件目录和月份；6.A清单、合成声明、取消及导入预算
    输出：已登记快照DTO，失败或取消不发布半成品
    """
    control = ImportControl(cancel, timeout_seconds)
    control.check()
    periods, audited, audit_bytes = validate_inputs(input_dir, periods, audit_manifest, synthetic)
    engine = create_metadata_engine(settings)
    root = settings.storage_dir.resolve() / "datasets"
    temporary = root / (".import-" + new_id("stage"))
    snapshot_id = new_id("snapshot")
    target = root / snapshot_id
    connection = None
    committed = False
    try:
        if not root.resolve().is_relative_to(settings.storage_dir.resolve()):
            raise DataError("INVALID_STORAGE_PATH")
        if inspect_database(engine)[0] != "ready":
            raise DataError("MIGRATION_REQUIRED")
        temporary.mkdir(parents=True, exist_ok=False)
        connection = duckdb.connect(str(temporary / "analytics.duckdb"))
        with control.watch(connection):
            columns = "record_id VARCHAR, source_row_number BIGINT, " + ", ".join(
                f'"{name}" {kind}' for _, name, kind, _ in FIELDS
            )
            connection.execute("CREATE TABLE month_stage (" + columns + ")")
            connection.execute(
                "CREATE TABLE raw_flights (source_file_id VARCHAR, "
                "source_period VARCHAR, " + columns + ")"
            )
            sources, qualities, hashes = [], {}, set()
            for period in periods:
                control.check()
                directory = temporary / "sources" / period
                directory.mkdir(parents=True)
                entry = audited.get(period)
                source_hash, mapping, quality = prepare_month(
                    input_dir / (period + ".csv"),
                    directory,
                    period,
                    synthetic=synthetic,
                    check=control.check,
                    expected_sha256=entry["sha256_before"] if entry else None,
                )
                if source_hash in hashes:
                    raise DataError("DUPLICATE_SOURCE_FILE")
                hashes.add(source_hash)
                if quality["out_of_period"]:
                    raise DataError("MONTH_DATE_OVERLAP")
                # 清单只作独立对照，实际值以本次冻结原件重算为准。
                if entry:
                    comparisons = (
                        quality["rows_raw"] == entry["counts"]["rows_raw"],
                        quality["null_counts"] == entry["null_counts"],
                        quality["negative_duration_counts"] == entry["negative_duration_counts"],
                        quality["duplicate_candidates"] == entry["counts"]["normalized_duplicates"],
                    )
                    if not all(comparisons):
                        raise DataError("AUDIT_RESULT_MISMATCH")
                source_id = "file_" + digest({"period": period, "sha256": source_hash})
                control.check()
                connection.execute("DELETE FROM month_stage")
                connection.execute(
                    "COPY month_stage FROM ? (HEADER TRUE, NULL '')",
                    [str(directory / "normalized.csv")],
                )
                connection.execute(
                    "INSERT INTO raw_flights SELECT ?, ?, * FROM month_stage", [source_id, period]
                )
                stats = verify_month(connection, period, quality)
                if not synthetic and set(stats["daily_counts"]) != set(month_days(period)):
                    raise DataError("MONTH_DATE_COVERAGE_INCOMPLETE")
                if entry and (
                    stats["cancelled"] != entry["flag_counts"]["cancelled"].get("1", 0)
                    or stats["diverted"] != entry["flag_counts"]["diverted"].get("1", 0)
                ):
                    raise DataError("AUDIT_RESULT_MISMATCH")
                sources.append(
                    {
                        "source_file_id": source_id,
                        "period": period,
                        "file": f"sources/{period}/source.csv",
                        "original_name": period + ".csv",
                        "sha256": source_hash,
                        "bytes": (directory / "source.csv").stat().st_size,
                        "rows": quality["rows_raw"],
                        "column_mapping": mapping,
                        **stats,
                        "provenance": entry["source"]
                        if entry
                        else {
                            "provenance_status": "synthetic_test_fixture",
                            "acquired_at": None,
                        },
                    }
                )
                qualities[period] = quality
                (directory / "normalized.csv").unlink()
                print(f"{period}: {quality['rows_raw']} rows loaded and checked", flush=True)
            connection.execute("DROP TABLE month_stage")
            connection.execute("CREATE VIEW v_flights AS SELECT * FROM raw_flights")
            row_count = sum(q["rows_raw"] for q in qualities.values())
            actual, unique_ids = connection.execute(
                "SELECT count(*), count(DISTINCT record_id) FROM v_flights"
            ).fetchone()
            if actual != row_count or unique_ids != row_count:
                raise DataError("LOADED_TOTAL_MISMATCH")
            described = connection.execute("DESCRIBE v_flights").fetchall()
            names = [column[0] for column in described]
            sample = connection.execute(
                "SELECT * FROM v_flights ORDER BY source_period, source_row_number LIMIT 5"
            ).fetchall()
            descriptions = {
                **DESCRIPTIONS,
                "source_file_id": "固定来源文件标识",
                "source_period": "来源月份",
                "source_row_number": "原始CSV逻辑记录号",
            }
            tables = [
                {
                    "name": "v_flights",
                    "columns": [
                        {"name": name, "type": kind, "description": descriptions.get(name, "")}
                        for name, kind, *_ in described
                    ],
                    "sample": [
                        dict(zip(names, map(encode_cell, row), strict=True)) for row in sample
                    ],
                    "join_keys": ["carrier_id", "origin_airport_id", "dest_airport_id"],
                }
            ]
            connection.execute("CHECKPOINT")
        connection.close()
        connection = None
        # 聚合质量只汇总计数，逐月异常示例仍带有清晰来源月份。
        nulls, negatives = Counter(), Counter()
        for quality in qualities.values():
            nulls.update(quality["null_counts"])
            negatives.update(quality["negative_duration_counts"])
        quality_report = {
            "rows_raw": row_count,
            "rows_exposed": row_count,
            "invalid_rows": 0,
            "out_of_period": 0,
            "null_counts": dict(nulls),
            "negative_duration_counts": dict(negatives),
            "months": qualities,
            "duplicate_candidates": sum(q["duplicate_candidates"] for q in qualities.values()),
            "cancelled_diverted_overlap": sum(
                q["cancelled_diverted_overlap"] for q in qualities.values()
            ),
            "arrival_threshold_mismatches": sum(
                q["arrival_threshold_mismatches"] for q in qualities.values()
            ),
            "policy": "保留来源、原行及异常；不去重、不填零；跨月指标适用期尚未审核。",
        }
        end = date.fromisoformat(periods[-1] + "-01")
        end = date(end.year + (end.month == 12), end.month % 12 + 1, 1)
        schema = {"snapshot_id": snapshot_id, "tables": tables}
        schema_hash = digest([{k: t[k] for k in ("name", "columns", "join_keys")} for t in tables])
        # 集合哈希覆盖有序来源身份，不冒充某一个原始CSV的哈希。
        collection_hash = digest([{k: s[k] for k in ("period", "sha256")} for s in sources])
        created = utc_now()
        manifest = {
            "schema_version": "bts_reporting_multimonth_v1",
            "period": None,
            "periods": periods,
            "coverage_start": periods[0] + "-01",
            "coverage_end_exclusive": end.isoformat(),
            "sources": sources,
            "source_kind": "synthetic" if synthetic else "official_bts",
            "source_collection_sha256": collection_hash,
            "rows_raw": row_count,
            "rows_exposed": row_count,
            "quality_report_hash": digest(quality_report),
            "schema_hash": schema_hash,
            "imported_at": created,
            "importer_commit": importer_version(),
            "audit_sha256": digest(json.loads(audit_bytes)) if audit_bytes else None,
            "rule_document_revisions": [],
            "dimensions": {},
            "policy": quality_report["policy"],
            "investigation_ready": False,
        }
        for name, value in (
            ("schema", schema),
            ("manifest", manifest),
            ("quality", quality_report),
        ):
            (temporary / (name + ".json")).write_bytes(canonical_bytes(value))
        if audit_bytes:
            (temporary / "audit.json").write_bytes(audit_bytes)
        control.check()
        temporary.rename(target)
        control.check()
        snapshot = Catalog(engine, settings.storage_dir).register(
            {
                "id": snapshot_id,
                "period": None,
                "row_count": row_count,
                "file_hash": collection_hash,
                "schema_hash": schema_hash,
                "created_at": created,
                "manifest": manifest,
                "quality_report": quality_report,
            },
            schema,
            manifest["source_kind"],
        )
        committed = True
        return snapshot
    except (duckdb.Error, OSError, UnicodeError, csv.Error, KeyError, TypeError):
        control.check()
        raise DataError("IMPORT_FAILED") from None
    finally:
        if connection is not None:
            connection.close()
        # 仅删除本次UUID目录；注册提交后即使调用方中断，也保留完整快照。
        if not committed and target.exists():
            with engine.connect() as c:
                committed = (
                    c.execute(select(DataSnapshot.id).where(DataSnapshot.id == snapshot_id)).first()
                    is not None
                )
        for directory in (temporary, target):
            if directory == target and committed:
                continue
            if directory.exists() and directory.resolve().is_relative_to(root.resolve()):
                shutil.rmtree(directory)
        engine.dispose()


def main():
    """
    作用：提供多月导入CLI，Ctrl+C通过事件取消实际工作，不提供自动激活
    输入：命令行月度目录、月份、审计清单及导入预算
    输出：快照JSON或安全错误
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("data/bts"))
    parser.add_argument("--periods", nargs="+", required=True)
    parser.add_argument("--audit-manifest", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args()
    cancel = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        result = ingest_months(
            Settings(),
            args.input_dir,
            args.periods,
            audit_manifest=args.audit_manifest,
            synthetic=args.synthetic,
            timeout_seconds=args.timeout_seconds,
            cancel=cancel,
        )
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))
    except DataError as exc:
        print(json.dumps({"error": {"code": exc.code, "details": exc.details}}))
        raise SystemExit(1) from None
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    main()
