"""Reporting Carrier CSV 离线导入；导入连接与分析连接分离。"""

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, canonical_bytes, digest, new_id, utc_now
from metric_pilot.data.serialization import encode_cell
from metric_pilot.database import create_metadata_engine, inspect_database
from metric_pilot.settings import Settings

BTS_URL = "https://www.transtats.bts.gov/DL_SelectFields.aspx?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ"
# 这些映射和类型是项目契约，字段解释的权威来源仍是 BTS 字典。
FIELDS = [
    ("FlightDate", "flight_date", "DATE", "航班运行日期"),
    ("Reporting_Airline", "carrier_code", "VARCHAR", "Reporting Carrier 代码"),
    ("DOT_ID_Reporting_Airline", "carrier_id", "BIGINT", "Reporting Carrier DOT 标识"),
    ("Flight_Number_Reporting_Airline", "flight_number", "VARCHAR", "航班号，不是唯一键"),
    ("Tail_Number", "tail_number", "VARCHAR", "机尾号"),
    ("OriginAirportID", "origin_airport_id", "BIGINT", "出发机场标识"),
    ("DestAirportID", "dest_airport_id", "BIGINT", "到达机场标识"),
    ("Origin", "origin_code", "VARCHAR", "出发机场代码"),
    ("Dest", "dest_code", "VARCHAR", "到达机场代码"),
    ("CRSDepTime", "scheduled_dep_local", "VARCHAR", "计划出发地方时 HHMM"),
    ("CRSArrTime", "scheduled_arr_local", "VARCHAR", "计划到达地方时 HHMM"),
    ("DepTime", "actual_dep_local", "VARCHAR", "实际出发地方时 HHMM"),
    ("ArrTime", "actual_arr_local", "VARCHAR", "实际到达地方时 HHMM"),
    ("DepDelay", "departure_delay", "DOUBLE", "出发时间偏差，允许负值"),
    ("ArrDelay", "arrival_delay", "DOUBLE", "到达时间偏差，允许负值"),
    ("ArrDelayMinutes", "arrival_delay_nonnegative", "DOUBLE", "非负到达延误分钟"),
    ("ArrDel15", "arrival_delayed_15", "BIGINT", "到达延误至少 15 分钟标记"),
    ("Cancelled", "cancelled", "BIGINT", "取消标记"),
    ("Diverted", "diverted", "BIGINT", "备降标记"),
    ("CancellationCode", "cancellation_code", "VARCHAR", "取消原因代码"),
    ("Distance", "distance_miles", "DOUBLE", "航距英里"),
    ("AirTime", "air_time", "DOUBLE", "空中时间分钟"),
    ("CRSElapsedTime", "scheduled_elapsed", "DOUBLE", "计划时长分钟"),
    ("ActualElapsedTime", "actual_elapsed", "DOUBLE", "实际时长分钟"),
    ("CarrierDelay", "delay_carrier", "DOUBLE", "已上报航司原因分钟"),
    ("WeatherDelay", "delay_weather", "DOUBLE", "已上报天气原因分钟"),
    ("NASDelay", "delay_nas", "DOUBLE", "已上报 NAS 原因分钟"),
    ("SecurityDelay", "delay_security", "DOUBLE", "已上报安全原因分钟"),
    ("LateAircraftDelay", "delay_late_aircraft", "DOUBLE", "已上报前序飞机原因分钟"),
]
DESCRIPTIONS = {name: description for _, name, _, description in FIELDS}
# BTS 字段选择页标签与实际 T_ONTIME_REPORTING 导出列名的对应关系。
HEADER_ALIASES = {
    "FlightDate": ("FL_DATE",),
    "Reporting_Airline": ("OP_UNIQUE_CARRIER",),
    "DOT_ID_Reporting_Airline": ("OP_CARRIER_AIRLINE_ID",),
    "Flight_Number_Reporting_Airline": ("OP_CARRIER_FL_NUM",),
    "Tail_Number": ("TAIL_NUM",),
    "ArrDelayMinutes": ("ARR_DELAY_NEW",),
}
DURATION_COLUMNS = {"air_time", "scheduled_elapsed", "actual_elapsed"}


def normalize_header(value: str) -> str:
    """
    作用：匹配官方列名的大小写和下划线差异
    输入：原始列名
    输出：用于映射的规范键
    """
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def parse_value(value: str, name: str, kind: str):
    """
    作用：严格解析单个 CSV 字段并保留业务日期与 HHMM
    输入：原文、内部名称、类型
    输出：规范值或 null
    """
    value = value.strip()
    if not value:
        if name in {
            "flight_date",
            "carrier_id",
            "carrier_code",
            "origin_airport_id",
            "dest_airport_id",
            "origin_code",
            "dest_code",
            "cancelled",
            "diverted",
        }:
            raise ValueError("required")
        return None
    if kind == "DATE":
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                # 官方字段选择下载使用月/日/年与 12 小时午夜，不依赖系统日期猜测。
                parsed = datetime.strptime(value, "%m/%d/%Y %I:%M:%S %p")
            if parsed.time().isoformat() != "00:00:00" or parsed.tzinfo:
                raise ValueError("date") from None
            return parsed.date().isoformat()
    if name.endswith("_local"):
        number = Decimal(value)
        if number != number.to_integral_value():
            raise ValueError("hhmm")
        hhmm = int(number)
        if hhmm < 0 or hhmm > 2400 or hhmm % 100 >= 60:
            raise ValueError("hhmm")
        return f"{hhmm:04d}"
    if kind in {"BIGINT", "DOUBLE"}:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("nonfinite")
        if kind == "BIGINT":
            if number != number.to_integral_value():
                raise ValueError("integer")
            number = int(number)
            if name in {"cancelled", "diverted", "arrival_delayed_15"} and number not in (0, 1):
                raise ValueError("flag")
            if name.endswith("_id") and number <= 0:
                raise ValueError("id")
            return number
        result = float(number)
        if not math.isfinite(result):
            raise ValueError("nonfinite")
        if name not in {"arrival_delay", "departure_delay", *DURATION_COLUMNS} and result < 0:
            raise ValueError("negative")
        return result
    return value


def copy_hashed(source: Path, target: Path, *, check=None) -> str:
    """
    作用：冻结实际输入字节，同时计算哈希避免读取期间替换
    输入：输入文件和工作目录内目标
    输出：源内容 SHA-256
    """
    checksum = hashlib.sha256()
    with source.open("rb") as reader, target.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            if check:
                check()
            checksum.update(chunk)
            writer.write(chunk)
    return checksum.hexdigest()


def importer_version() -> str:
    """
    作用：记录实际源码提交或未提交状态
    输入：无
    输出：提交标识或明确未知标记
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=3, check=True
        ).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unversioned"


def import_dimension(connection, source: Path, table: str, key: str, target: Path) -> dict:
    """
    作用：导入唯一键代码表，禁止重复键扩张事实数据
    输入：导入连接、CSV、固定表名和键名、冻结文件目标
    输出：代码表哈希和行数
    """
    file_hash = copy_hashed(source, target)
    rows, keys = [], set()
    with target.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != [key, "name"]:
            raise DataError("DIMENSION_COLUMNS_REQUIRED", {"columns": [key, "name"]})
        for row in reader:
            try:
                identifier = int(row[key])
                if identifier <= 0 or not row["name"].strip():
                    raise ValueError()
            except (ValueError, TypeError):
                raise DataError("INVALID_DIMENSION") from None
            if identifier in keys:
                raise DataError("DUPLICATE_DIMENSION_KEY", {"table": table})
            keys.add(identifier)
            rows.append((identifier, row["name"].strip()))
    connection.execute(f"CREATE TABLE {table} ({key} BIGINT PRIMARY KEY, name VARCHAR NOT NULL)")
    if rows:
        connection.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
    return {"sha256": file_hash, "rows": len(rows)}


def prepare_month(
    input_path: Path,
    temporary: Path,
    period: str,
    *,
    synthetic=False,
    check=None,
    expected_sha256=None,
):
    """
    作用：冻结并规范化单个月文件，不登记快照
    输入：原件、已创建临时目录、月份、合成声明、取消检查和预期哈希
    输出：原件哈希、字段映射和质量报告
    """
    source_hash = copy_hashed(input_path, temporary / "source.csv", check=check)
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise DataError("SOURCE_HASH_MISMATCH")
    nulls, duplicates, raw_count, exposed_count, out_of_period = Counter(), set(), 0, 0, 0
    duplicate_count, overlaps, mismatches = 0, 0, 0
    negative_durations = Counter({name: 0 for name in sorted(DURATION_COLUMNS)})
    negative_duration_examples = {name: [] for name in sorted(DURATION_COLUMNS)}
    normalized = temporary / "normalized.csv"
    with (
        (temporary / "source.csv").open(encoding="utf-8-sig", newline="") as stream,
        normalized.open("w", encoding="utf-8", newline="") as output,
    ):
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        normalized_headers = [normalize_header(h) for h in headers]
        if len(set(normalized_headers)) != len(normalized_headers):
            raise DataError("DUPLICATE_COLUMNS")
        if any("MARKETING" in h or h.startswith(("OPERATING", "MKT")) for h in normalized_headers):
            raise DataError("MARKETING_CARRIER_NOT_ALLOWED")
        if "SYNTHETICFIXTURE" in normalized_headers and not synthetic:
            raise DataError("SYNTHETIC_FLAG_REQUIRED")
        lookup = dict(zip(normalized_headers, headers, strict=True))
        for original, *_ in FIELDS:
            candidates = [
                key
                for name in (original, *HEADER_ALIASES.get(original, ()))
                if (key := normalize_header(name)) in lookup
            ]
            if len(candidates) > 1:
                raise DataError("AMBIGUOUS_COLUMNS", {"column": original})
            if candidates:
                lookup[normalize_header(original)] = lookup[candidates[0]]
        missing = [original for original, *_ in FIELDS if normalize_header(original) not in lookup]
        if missing:
            raise DataError("MISSING_COLUMNS", {"columns": missing})
        mapping = {lookup[normalize_header(original)]: name for original, name, *_ in FIELDS}
        writer = csv.writer(output)
        writer.writerow(["record_id", "source_row_number"] + [name for _, name, *_ in FIELDS])
        for row_number, row in enumerate(reader, start=2):
            if check and raw_count % 1000 == 0:
                check()
            raw_count += 1
            if None in row or any(v is None for v in row.values()):
                raise DataError("INVALID_CSV_ROW", {"row": row_number})
            values = []
            for original, name, kind, _ in FIELDS:
                try:
                    value = parse_value(row[lookup[normalize_header(original)]], name, kind)
                except (ValueError, InvalidOperation, OverflowError):
                    raise DataError(
                        "INVALID_FIELD", {"row": row_number, "column": original}
                    ) from None
                nulls[name] += value is None
                values.append(value)
            record = dict(zip([name for _, name, *_ in FIELDS], values, strict=True))
            # 源文件中的负时长不重写、不删行；与允许为负的延误偏差分开质检。
            for name in DURATION_COLUMNS:
                if record[name] is not None and record[name] < 0:
                    negative_durations[name] += 1
                    if len(negative_duration_examples[name]) < 5:
                        negative_duration_examples[name].append(
                            {"source_row_number": row_number, "value": record[name]}
                        )
            row_hash = digest(values)
            duplicate_count += row_hash in duplicates
            duplicates.add(row_hash)
            overlaps += record["cancelled"] == record["diverted"] == 1
            if record["arrival_delay"] is not None and record["arrival_delayed_15"] is not None:
                mismatches += int(record["arrival_delay"] >= 15) != record["arrival_delayed_15"]
            if record["flight_date"][:7] == period:
                exposed_count += 1
            else:
                out_of_period += 1
            record_id = hashlib.sha256(f"{source_hash}:{row_number}".encode()).hexdigest()
            writer.writerow([record_id, row_number] + values)
    if raw_count == 0:
        raise DataError("EMPTY_INPUT")
    quality = {
        "rows_raw": raw_count,
        "rows_exposed": exposed_count,
        "out_of_period": out_of_period,
        "null_counts": dict(nulls),
        "duplicate_candidates": duplicate_count,
        "cancelled_diverted_overlap": overlaps,
        "arrival_threshold_mismatches": mismatches,
        "negative_duration_counts": dict(negative_durations),
        "negative_duration_examples": negative_duration_examples,
        "policy": (
            "保留原始行和重复候选；业务视图仅暴露所选月；"
            "不填充空值、不删除负偏差；负时长原值保留并单列质检"
        ),
        "invalid_rows": 0,
        "dimension_join_cardinality_verified": [],
    }
    return source_hash, mapping, quality


def ingest(
    settings: Settings,
    input_path: Path,
    period: str,
    *,
    synthetic: bool = False,
    downloaded_at: str | None = None,
    carriers: Path | None = None,
    airports: Path | None = None,
):
    """
    作用：验证并原子发布一个新的 Reporting Carrier 月度快照
    输入：配置、输入文件、月份、来源声明和可选代码表
    输出：实际登记的快照 DTO，不自动激活
    """
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise DataError("INVALID_PERIOD")
    if downloaded_at:
        try:
            timestamp = datetime.fromisoformat(downloaded_at)
            if timestamp.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise DataError("DOWNLOAD_TIME_REQUIRES_TIMEZONE") from None
    engine = create_metadata_engine(settings)
    root = settings.storage_dir.resolve() / "datasets"
    if not root.resolve().is_relative_to(settings.storage_dir.resolve()):
        raise DataError("INVALID_STORAGE_PATH")
    root.mkdir(parents=True, exist_ok=True)
    # mkdtemp 在 Windows 创建私有 ACL，重命名后会阻止正常服务账户读取。
    # UUID 临时目录继承同一工作区 datasets 的权限，发布后仍可由工作区服务访问。
    temporary = (root / f".import-{new_id('stage')}").resolve()
    connection = None
    try:
        temporary.mkdir()
        if inspect_database(engine)[0] != "ready":
            raise DataError("MIGRATION_REQUIRED")
        source_hash, mapping, quality = prepare_month(
            input_path, temporary, period, synthetic=synthetic
        )
        normalized = temporary / "normalized.csv"
        raw_count, exposed_count = quality["rows_raw"], quality["rows_exposed"]
        connection = duckdb.connect(str(temporary / "analytics.duckdb"))
        columns = "record_id VARCHAR, source_row_number BIGINT, " + ", ".join(
            f'"{name}" {kind}' for _, name, kind, _ in FIELDS
        )
        connection.execute(f"CREATE TABLE raw_flights ({columns})")
        connection.execute("COPY raw_flights FROM ? (HEADER TRUE, NULL '')", [str(normalized)])
        start = date.fromisoformat(period + "-01")
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        connection.execute(
            f"CREATE VIEW v_flights AS SELECT * FROM raw_flights WHERE "
            f"flight_date >= DATE '{start}' AND flight_date < DATE '{end}'"
        )
        dimensions = {}
        for source, table, key in [
            (carriers, "dim_carriers", "carrier_id"),
            (airports, "dim_airports", "airport_id"),
        ]:
            if source:
                dimensions[table] = import_dimension(
                    connection, source, table, key, temporary / f"{table}.csv"
                )
                links = (
                    ["origin_airport_id", "dest_airport_id"]
                    if airports == source and table == "dim_airports"
                    else ["carrier_id"]
                )
                for link in links:
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM v_flights f LEFT JOIN {table} d ON f.{link}=d.{key}"
                    ).fetchone()[0]
                    if count != exposed_count:
                        raise DataError("JOIN_CARDINALITY_CHANGED")
        snapshot_id = new_id("snapshot")
        tables = []
        for table in ["v_flights", *dimensions]:
            described = connection.execute(f"DESCRIBE {table}").fetchall()
            fields = [
                {"name": r[0], "type": r[1], "description": DESCRIPTIONS.get(r[0], "")}
                for r in described
            ]
            sample = connection.execute(f"SELECT * FROM {table} ORDER BY 1 LIMIT 5").fetchall()
            tables.append(
                {
                    "name": table,
                    "columns": fields,
                    "sample": [
                        {
                            col["name"]: encode_cell(value)
                            for col, value in zip(fields, row, strict=True)
                        }
                        for row in sample
                    ],
                    "join_keys": ["carrier_id", "origin_airport_id", "dest_airport_id"]
                    if table == "v_flights"
                    else ["carrier_id" if table == "dim_carriers" else "airport_id"],
                }
            )
        connection.close()
        connection = None
        normalized.unlink()
        quality["dimension_join_cardinality_verified"] = list(dimensions)
        schema = {"snapshot_id": snapshot_id, "tables": tables}
        schema_hash = digest([{k: t[k] for k in ("name", "columns", "join_keys")} for t in tables])
        created = utc_now()
        manifest = {
            "source": "SYNTHETIC Reporting Carrier fixture"
            if synthetic
            else "BTS Reporting Carrier On-Time Performance",
            "source_kind": "synthetic" if synthetic else "official_bts",
            "source_url": None if synthetic else BTS_URL,
            "period": period,
            "downloaded_at": downloaded_at,
            "imported_at": created,
            "source_sha256": source_hash,
            "schema_version": "bts_reporting_v1",
            "rows_raw": raw_count,
            "rows_exposed": exposed_count,
            "quality_report_hash": digest(quality),
            "schema_hash": schema_hash,
            "column_mapping": mapping,
            "dimensions": dimensions,
            "importer_commit": importer_version(),
            "rule_document_revisions": [],
            "policy": quality["policy"],
        }
        (temporary / "quality.json").write_bytes(canonical_bytes(quality))
        (temporary / "manifest.json").write_bytes(canonical_bytes(manifest))
        (temporary / "schema.json").write_bytes(canonical_bytes(schema))
        # 临时目录完整关闭后才发布；新 UUID 目录不会覆盖旧快照。
        temporary.rename(root / snapshot_id)
        return Catalog(engine, settings.storage_dir).register(
            {
                "id": snapshot_id,
                "period": period,
                "row_count": exposed_count,
                "file_hash": source_hash,
                "schema_hash": schema_hash,
                "created_at": created,
                "manifest": manifest,
                "quality_report": quality,
            },
            schema,
            "synthetic" if synthetic else "official_bts",
        )
    except (duckdb.Error, OSError, UnicodeError, csv.Error):
        raise DataError("IMPORT_FAILED") from None
    finally:
        if connection:
            connection.close()
        # 只清理本次在指定 datasets 根下创建的临时目录。
        if temporary.exists() and temporary.is_relative_to(root.resolve()):
            shutil.rmtree(temporary)
        engine.dispose()


def main():
    """
    作用：提供真实 CSV 和合成数据共用的导入命令
    输入：命令行参数
    输出：安全 JSON 结果与退出码
    """
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="导入 BTS Reporting Carrier 月度 CSV")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--period", required=True)
    parser.add_argument("--synthetic", action="store_true", help="明确声明人工合成数据")
    parser.add_argument("--downloaded-at")
    parser.add_argument("--carriers", type=Path)
    parser.add_argument("--airports", type=Path)
    parser.add_argument("--activate", action="store_true", help="显式设为默认快照")
    args = parser.parse_args()
    settings = Settings()
    try:
        snapshot = ingest(
            settings,
            args.input,
            args.period,
            synthetic=args.synthetic,
            downloaded_at=args.downloaded_at,
            carriers=args.carriers,
            airports=args.airports,
        )
        if args.activate:
            engine = create_metadata_engine(settings)
            try:
                Catalog(engine, settings.storage_dir).activate(snapshot.source_id, snapshot.id)
            finally:
                engine.dispose()
        print(json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=True, allow_nan=False))
    except DataError as exc:
        print(json.dumps({"error": {"code": exc.code, "details": exc.details}}, ensure_ascii=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
