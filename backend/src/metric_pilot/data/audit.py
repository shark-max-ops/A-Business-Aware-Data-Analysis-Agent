"""阶段 6.A：只读核验 BTS 月度原件，不创建或登记分析数据库。"""

import argparse
import calendar
import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from metric_pilot.data.ingest import (
    BTS_URL,
    DURATION_COLUMNS,
    FIELDS,
    HEADER_ALIASES,
    normalize_header,
    parse_value,
)

KEY_COLUMNS = (
    "flight_date",
    "carrier_id",
    "flight_number",
    "origin_airport_id",
    "dest_airport_id",
    "scheduled_dep_local",
)
SAMPLE_LIMIT = 5


def checksum(path: Path) -> str:
    """
    作用：只读计算完整文件的字节哈希
    输入：path - 原件路径
    输出：SHA-256 字符串
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def row_digest(values: list) -> bytes:
    """
    作用：为字段序列生成无分隔符歧义的重复候选摘要
    输入：values - 原始或规范字段序列
    输出：SHA-256 字节摘要
    """
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).digest()


def month_days(period: str) -> list[str]:
    """
    作用：校验月份并列出实际日历中的全部日期
    输入：period - YYYY-MM 月份
    输出：ISO 日期列表，非法月份抛出 ValueError
    """
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("INVALID_PERIOD")
    start = date.fromisoformat(period + "-01")
    return [
        (start + timedelta(days=i)).isoformat()
        for i in range(calendar.monthrange(start.year, start.month)[1])
    ]


def inspect_header(headers: list[str]) -> dict:
    """
    作用：按现有导入契约检查必需列、别名冲突及不同表口径标记
    输入：headers - CSV 原始表头
    输出：字段映射、额外列和结构错误，不推定官方来源已证实
    """
    normalized = [normalize_header(h) for h in headers]
    positions = defaultdict(list)
    for index, key in enumerate(normalized):
        positions[key].append(index)
    errors, mapping = [], {}
    if len(normalized) != len(set(normalized)):
        errors.append("DUPLICATE_COLUMNS")
    if any("MARKETING" in h or h.startswith(("OPERATING", "MKT")) for h in normalized):
        errors.append("MARKETING_CARRIER_NOT_ALLOWED")
    if "SYNTHETICFIXTURE" in normalized:
        errors.append("SYNTHETIC_INPUT_NOT_OFFICIAL")
    missing, ambiguous = [], []
    for original, name, *_ in FIELDS:
        candidates = {
            index
            for alias in (original, *HEADER_ALIASES.get(original, ()))
            for index in positions.get(normalize_header(alias), [])
        }
        if not candidates:
            missing.append(original)
        elif len(candidates) > 1:
            ambiguous.append(original)
        else:
            mapping[name] = candidates.pop()
    if missing:
        errors.append("MISSING_COLUMNS")
    if ambiguous:
        errors.append("AMBIGUOUS_COLUMNS")
    used = set(mapping.values())
    return {
        "headers": headers,
        "mapping": mapping,
        "missing_columns": missing,
        "ambiguous_columns": ambiguous,
        "errors": errors,
        "extra_columns": [h for i, h in enumerate(headers) if i not in used],
    }


def inspect_month(path: Path, period: str) -> dict:
    """
    作用：全量读取一个月的 CSV，登记结构、日期、空值及重复候选
    输入：path - 原件路径；period - 预期月份
    输出：只读核验结果，坏行和不完整扫描显式保留
    """
    expected_dates = month_days(period)
    started = time.monotonic()
    before = checksum(path)
    size = path.stat().st_size
    counts, dates = Counter(), Counter()
    nulls = Counter({name: 0 for _, name, *_ in FIELDS})
    invalid = Counter({name: 0 for _, name, *_ in FIELDS})
    negative = Counter({name: 0 for name in sorted(DURATION_COLUMNS)})
    examples = defaultdict(list)
    raw_seen, normalized_seen, keys_seen = set(), set(), set()
    flags = {name: Counter() for name in ("cancelled", "diverted")}
    header, scan_complete, failure = inspect_header([]), False, None

    # 示例只记行定位，不把真实明细复制到源码或日志。
    def note(kind: str, row_number: int, line_end: int) -> None:
        """
        作用：限制异常定位样本大小，计数仍覆盖全量
        输入：异常名称、逻辑记录号与物理结束行
        输出：无，更新样本容器
        """
        if len(examples[kind]) < SAMPLE_LIMIT:
            examples[kind].append({"source_row_number": row_number, "physical_line_end": line_end})

    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            header = inspect_header(next(reader, []))
            can_parse = len(header["mapping"]) == len(FIELDS) and not any(
                e in header["errors"] for e in ("DUPLICATE_COLUMNS", "AMBIGUOUS_COLUMNS")
            )
            for row_number, row in enumerate(reader, start=2):
                counts["rows_raw"] += 1
                if len(row) != len(header["headers"]):
                    counts["malformed_rows"] += 1
                    note("malformed_rows", row_number, reader.line_num)
                    continue
                counts["rows_well_formed"] += 1
                raw_hash = row_digest(row)
                if raw_hash in raw_seen:
                    counts["exact_field_duplicates"] += 1
                    note("exact_field_duplicates", row_number, reader.line_num)
                raw_seen.add(raw_hash)
                if not can_parse:
                    continue

                # 每个字段独立检查，某字段非法时仍保留其他字段的统计。
                record = {}
                for _, name, kind, _ in FIELDS:
                    text = row[header["mapping"][name]]
                    nulls[name] += not text.strip()
                    try:
                        value = parse_value(text, name, kind)
                        if kind == "BIGINT" and value is not None and not -(2**63) <= value < 2**63:
                            raise ValueError("int64")
                        record[name] = value
                    except (ValueError, InvalidOperation, OverflowError):
                        invalid[name] += 1
                        note("invalid_" + name, row_number, reader.line_num)
                if len(record) != len(FIELDS):
                    counts["invalid_field_rows"] += 1
                else:
                    counts["rows_parsed"] += 1
                    normalized_hash = row_digest([record[name] for _, name, *_ in FIELDS])
                    if normalized_hash in normalized_seen:
                        counts["normalized_duplicates"] += 1
                        note("normalized_duplicates", row_number, reader.line_num)
                    normalized_seen.add(normalized_hash)

                # 日期覆盖独立于其他字段是否合法，不将非法日期计入缺失分母。
                when = record.get("flight_date")
                if when:
                    dates[when] += 1
                    if when[:7] != period:
                        counts["out_of_period"] += 1
                        note("out_of_period", row_number, reader.line_num)
                if all(record.get(name) is not None for name in KEY_COLUMNS):
                    key_hash = row_digest([record[name] for name in KEY_COLUMNS])
                    if key_hash in keys_seen:
                        counts["flight_key_duplicate_candidates"] += 1
                        note("flight_key_duplicate_candidates", row_number, reader.line_num)
                    keys_seen.add(key_hash)
                for name in flags:
                    if name in record:
                        flags[name][str(record[name])] += 1
                for name in DURATION_COLUMNS:
                    if record.get(name) is not None and record[name] < 0:
                        negative[name] += 1
                        note("negative_" + name, row_number, reader.line_num)

                # 一致性计数是质量提示，不自动修改数据或批准指标。
                checks = {
                    "cancelled_diverted_overlap": record.get("cancelled") == 1
                    and record.get("diverted") == 1,
                    "arrival_threshold_mismatches": record.get("arrival_delay") is not None
                    and record.get("arrival_delayed_15") is not None
                    and int(record["arrival_delay"] >= 15) != record["arrival_delayed_15"],
                    "nonnegative_delay_mismatches": record.get("arrival_delay") is not None
                    and record.get("arrival_delay_nonnegative") is not None
                    and max(0, record["arrival_delay"]) != record["arrival_delay_nonnegative"],
                    "negative_arrivals": record.get("arrival_delay") is not None
                    and record["arrival_delay"] < 0,
                    "eligible_missing_arrival_delay": record.get("cancelled") == 0
                    and record.get("diverted") == 0
                    and "arrival_delay" in record
                    and record["arrival_delay"] is None,
                }
                for name, found in checks.items():
                    counts[name] += bool(found)
                    if found and name != "negative_arrivals":
                        note(name, row_number, reader.line_num)
            scan_complete = True
    except (csv.Error, UnicodeError) as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)}

    # 扫描前后重新哈希；变化中的文件不能成为稳定核验结果。
    after = checksum(path)
    missing_dates = sorted(set(expected_dates) - dates.keys())
    blockers = list(header["errors"])
    for name in ("malformed_rows", "invalid_field_rows", "out_of_period"):
        if counts[name]:
            blockers.append(name.upper())
    if not counts["rows_raw"]:
        blockers.append("EMPTY_INPUT")
    if missing_dates:
        blockers.append("MISSING_DATES")
    if not scan_complete:
        blockers.append("INCOMPLETE_SCAN")
    if before != after or size != path.stat().st_size:
        blockers.append("SOURCE_CHANGED_DURING_SCAN")
    count_names = (
        "rows_raw",
        "rows_well_formed",
        "rows_parsed",
        "malformed_rows",
        "invalid_field_rows",
        "out_of_period",
        "exact_field_duplicates",
        "normalized_duplicates",
        "flight_key_duplicate_candidates",
        "cancelled_diverted_overlap",
        "arrival_threshold_mismatches",
        "nonnegative_delay_mismatches",
        "negative_arrivals",
        "eligible_missing_arrival_delay",
    )
    return {
        "period": period,
        "file_name": path.name,
        "bytes": size,
        "sha256_before": before,
        "sha256_after": after,
        "source_unchanged": before == after,
        "source": {
            "expected_table": "BTS Reporting Carrier On-Time Performance",
            "table_basis": "expected_contract_only",
            "reference_download_url": BTS_URL,
            "acquired_at": None,
            "provenance_status": "user_provided_not_independently_authenticated",
            "geographic_selection": "unknown",
            "download_receipt": None,
            "note": "字段匹配不证明下载来源；获取时间、下载选项及原始下载凭据未知。",
        },
        "schema": header,
        "scan_complete": scan_complete,
        "scan_error": failure,
        "technical_blockers": blockers,
        "technical_checks_passed": not blockers,
        "counts": {name: counts[name] for name in count_names},
        "null_counts": dict(nulls),
        "invalid_field_counts": dict(invalid),
        "negative_duration_counts": dict(negative),
        "flag_counts": {name: dict(value) for name, value in flags.items()},
        "date_min": min(dates, default=None),
        "date_max": max(dates, default=None),
        "daily_counts": dict(sorted(dates.items())),
        "missing_dates": missing_dates,
        "examples": dict(examples),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def audit_months(
    directory: Path, periods: list[str], *, user_confirmed_reporting_all: bool = False
) -> dict:
    """
    作用：核验选定月份及相互关系，不访问应用存储或联网
    输入：directory - 月度原件目录；periods - 月份；user_confirmed_reporting_all - 用户来源声明
    输出：完整本地清单，缺文件不创建占位
    """
    if not periods or len(set(periods)) != len(periods):
        raise ValueError("UNIQUE_PERIODS_REQUIRED")
    for period in periods:
        month_days(period)
    periods = sorted(periods)
    months, missing = [], []
    for period in periods:
        path = directory / (period + ".csv")
        if not path.is_file():
            missing.append(period)
            continue
        months.append(inspect_month(path, period))
        print(f"{period}: {months[-1]['counts']['rows_raw']} rows scanned", flush=True)

    # 用户声明绑定本次文件哈希，与独立认证和真实下载时间分开登记。
    if user_confirmed_reporting_all:
        for month in months:
            month["source"].update(
                {
                    "table_basis": "user_statement",
                    "provenance_status": "user_confirmed_not_independently_authenticated",
                    "geographic_selection": "all",
                    "note": (
                        "用户确认来自Reporting Carrier、地理范围全部；"
                        "下载时间未知，未独立认证来源。"
                    ),
                }
            )

    # 比较实际日期集合而不把拼接文件当作已支持跨月查询。
    overlaps, repeated_files = [], []
    for index, left in enumerate(months):
        for right in months[index + 1 :]:
            dates = sorted(left["daily_counts"].keys() & right["daily_counts"].keys())
            if dates:
                overlaps.append({"periods": [left["period"], right["period"]], "dates": dates})
            if left["sha256_before"] == right["sha256_before"]:
                repeated_files.append([left["period"], right["period"]])
    calendar_months = []
    cursor = date.fromisoformat(periods[0] + "-01")
    while cursor.isoformat()[:7] <= periods[-1]:
        calendar_months.append(cursor.isoformat()[:7])
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    gaps = sorted(set(calendar_months) - set(periods))
    signatures = {
        tuple(sorted(normalize_header(h) for h in month["schema"]["headers"])) for month in months
    }
    return {
        "schema_version": 1,
        "stage": "6.A",
        "audited_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "expected_periods": periods,
        "missing_files": missing,
        "unselected_gap_months": gaps,
        "months": months,
        "duplicate_files": repeated_files,
        "overlapping_dates": overlaps,
        "same_normalized_header_set": len(signatures) == 1,
        "technical_checks_passed": not (missing or gaps or overlaps or repeated_files)
        and all(m["technical_checks_passed"] for m in months),
        "provenance_verified": False,
        "database_imported": False,
        "policy": {
            "scope": "仅原件离线核验，不登记快照、不切换默认数据、不批准指标。",
            "nulls": "逐字段统计空白，不填零；缺列或坏行不计入该字段空值分母。",
            "duplicates": "摘要比较全字段/规范29字段；航班候选键不是唯一键，不删行。",
            "flight_candidate_key": list(KEY_COLUMNS),
            "counts": "重复计数为每组首条之外的行数；统计范围为格式完整且对应字段可解析的行。",
            "dates": "缺少日记录不证明官方数据遗漏；仅报告预期日历与文件日期的差异。",
            "warnings": "负时长、一致性问题、缺失及来源未知需在6.B前复核；不代表官方数据正确。",
        },
    }


def render_summary(report: dict) -> str:
    """
    作用：生成与机器清单一致的人可读核验摘要
    输入：report - 月度核验清单
    输出：Markdown 文本
    """
    lines = [
        "# BTS 月度原件核验（阶段 6.A）",
        "",
        f"核验时间：{report['audited_at']}",
        "",
        "仅核验原 CSV；未导入数据库，未创建或激活快照。下载凭据与获取时间尚未核实。",
        "",
        "| 月份 | 行数 | 日期起止 | 缺日 | 取消 | 备降 | 规范重复候选 | 技术阻断 |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for month in report["months"]:
        lines.append(
            f"| {month['period']} | {month['counts']['rows_raw']:,} | "
            f"{month['date_min']}—{month['date_max']} | {len(month['missing_dates'])} | "
            f"{month['flag_counts']['cancelled'].get('1', 0):,} | "
            f"{month['flag_counts']['diverted'].get('1', 0):,} | "
            f"{month['counts']['normalized_duplicates']:,} | "
            f"{', '.join(month['technical_blockers']) or '无'} |"
        )
    lines += [
        "",
        f"缺文件：{report['missing_files']}；未选缺月：{report['unselected_gap_months']}。",
        f"重复文件：{report['duplicate_files']}；跨文件重叠日期：{report['overlapping_dates']}。",
        "",
        "空值、非法值、负时长、标记一致性、每日行数、哈希及异常行定位详见 manifest.json。",
        "重复候选不删除；负到达偏差允许，负时长另作质量提示。",
        "技术检查通过也不代表来源已认证、业务口径已审核或跨月调查已实现。",
        "",
    ]
    for month in report["months"]:
        lines += [
            f"## {month['period']} 质量与来源备注",
            "",
            month["source"]["note"],
            "",
            f"SHA-256：`{month['sha256_before']}`；扫描前后哈希一致：{month['source_unchanged']}。",
            f"负时长计数：{month['negative_duration_counts']}；"
            f"计划时长空白：{month['null_counts']['scheduled_elapsed']}。",
            f"到达偏差空白：{month['null_counts']['arrival_delay']}；"
            f"未取消且未备降但缺到达偏差：{month['counts']['eligible_missing_arrival_delay']}。",
            f"航班候选键重复：{month['counts']['flight_key_duplicate_candidates']}；"
            f"到达阈值不一致：{month['counts']['arrival_threshold_mismatches']}；"
            f"非负延误映射不一致：{month['counts']['nonnegative_delay_mismatches']}。",
            "",
        ]
    return "\n".join(lines)


def main() -> None:
    """
    作用：从命令行执行离线核验并独占创建一次核验记录
    输入：命令行原件目录、月份及尚不存在的输出目录
    输出：本地 JSON/Markdown；技术阻断退出2，正常核验退出0
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("data/bts"))
    parser.add_argument("--periods", nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--user-confirmed-reporting-all",
        action="store_true",
        help="仅在用户明确确认所选文件来自Reporting Carrier且地理范围全部时使用；不代表独立认证",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    source = args.input_dir.resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("OUTPUT_MUST_BE_SEPARATE_FROM_SOURCE")
    if output.exists():
        parser.error("OUTPUT_ALREADY_EXISTS: use a new directory to preserve previous audits")
    try:
        report = audit_months(
            source, args.periods, user_confirmed_reporting_all=args.user_confirmed_reporting_all
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "summary.md").write_text(render_summary(report), encoding="utf-8")
    print(f"Audit saved: {output}")
    raise SystemExit(0 if report["technical_checks_passed"] else 2)


if __name__ == "__main__":
    main()
