"""阶段 6.A 合成样本：原件只读、质量计数、缺月与失败记录。"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from metric_pilot.data.audit import (
    audit_months,
    checksum,
    inspect_header,
    inspect_month,
    month_days,
    render_summary,
)
from metric_pilot.data.ingest import FIELDS

ROOT = Path(__file__).resolve().parents[2]


def synthetic_row(day="2026-02-01", **changes):
    """
    作用：构造明确为合成、人工可计算的29字段记录
    输入：day - 日期；changes - 按原始字段名称覆盖的值
    输出：字段名称到字符串的字典，仅用于测试
    """
    row = {original: "" for original, *_ in FIELDS}
    row.update(
        {
            "FlightDate": day,
            "Reporting_Airline": "ZZ",
            "DOT_ID_Reporting_Airline": "90001",
            "Flight_Number_Reporting_Airline": "1",
            "OriginAirportID": "99001",
            "DestAirportID": "99002",
            "Origin": "ZZZ",
            "Dest": "XXX",
            "CRSDepTime": "0900",
            "ArrDelay": "-10",
            "ArrDelayMinutes": "0",
            "ArrDel15": "0",
            "Cancelled": "0",
            "Diverted": "0",
            "AirTime": "60",
        }
    )
    row.update(changes)
    return row


def write_month(path, rows, headers=None):
    """
    作用：向临时目录写入明确合成的CSV，避免触碰真实原件
    输入：path - 临时路径；rows - 合成记录；headers - 可选表头
    输出：写入路径
    """
    headers = headers if headers is not None else [original for original, *_ in FIELDS]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows([row.get(h, "") for h in headers] for row in rows)
    return path


def test_full_month_quality_and_original_unchanged(tmp_path):
    """
    作用：独立期望值验证重复、空值、质量问题及来源未知，不删除异常
    输入：pytest 临时目录
    输出：无，检查人工计算的计数和原件哈希
    """
    rows = [synthetic_row(day) for day in month_days("2026-02")]
    rows += [rows[0].copy(), synthetic_row("2026-02-01", AirTime="-1", Cancelled="1")]
    rows += [synthetic_row("2026-02-02", Diverted="1", Cancelled="1", ArrDel15="1")]
    rows += [synthetic_row("2026-02-03", ArrDelayMinutes="4", ArrDelay="", Tail_Number="")]
    path = write_month(tmp_path / "2026-02.csv", rows)
    before = path.read_bytes()
    result = inspect_month(path, "2026-02")
    assert result["technical_checks_passed"]
    assert result["counts"]["rows_raw"] == 32
    assert result["counts"]["exact_field_duplicates"] == 1
    assert result["counts"]["normalized_duplicates"] == 1
    assert result["counts"]["flight_key_duplicate_candidates"] == 4
    assert result["counts"]["arrival_threshold_mismatches"] == 1
    assert result["counts"]["cancelled_diverted_overlap"] == 1
    assert result["counts"]["eligible_missing_arrival_delay"] == 1
    assert result["null_counts"]["tail_number"] == 32
    assert result["negative_duration_counts"]["air_time"] == 1
    assert result["flag_counts"]["cancelled"] == {"0": 30, "1": 2}
    assert len(result["daily_counts"]) == 28
    assert result["source"]["acquired_at"] is None
    assert result["sha256_before"] == result["sha256_after"] == checksum(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "headers,error",
    [
        ([f[0] for f in FIELDS][1:], "MISSING_COLUMNS"),
        ([f[0] for f in FIELDS] + ["FL_DATE"], "AMBIGUOUS_COLUMNS"),
        ([f[0] for f in FIELDS] + ["Flight_Date"], "DUPLICATE_COLUMNS"),
        ([f[0] for f in FIELDS] + ["Marketing_Airline"], "MARKETING_CARRIER_NOT_ALLOWED"),
        ([f[0] for f in FIELDS] + ["SyntheticFixture"], "SYNTHETIC_INPUT_NOT_OFFICIAL"),
    ],
)
def test_header_rejections(headers, error):
    """
    作用：阻止缺列、重复列、别名冲突及错误表口径静默通过
    输入：合成表头和预期错误
    输出：无
    """
    assert error in inspect_header(headers)["errors"]


def test_invalid_values_dates_rows_and_bounded_examples(tmp_path):
    """
    作用：验证坏值不填零、越月记录不丢弃、异常样本截断不影响计数
    输入：pytest 临时目录
    输出：无
    """
    rows = [synthetic_row(ArrDelay="NaN", Cancelled="2", DOT_ID_Reporting_Airline=str(2**63))]
    rows += [synthetic_row("invalid-date"), synthetic_row("2026-03-01")]
    rows += [synthetic_row("2026-02-01", AirTime="-1")] * 7
    path = write_month(tmp_path / "2026-02.csv", rows)
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write("bad,row\n")
    result = inspect_month(path, "2026-02")
    assert not result["technical_checks_passed"]
    assert result["scan_complete"]
    assert result["counts"]["rows_raw"] == 11
    assert result["counts"]["malformed_rows"] == 1
    assert result["counts"]["invalid_field_rows"] == 2
    assert result["counts"]["out_of_period"] == 1
    assert result["invalid_field_counts"]["flight_date"] == 1
    assert result["invalid_field_counts"]["carrier_id"] == 1
    assert result["invalid_field_counts"]["cancelled"] == 1
    assert result["invalid_field_counts"]["arrival_delay"] == 1
    assert result["negative_duration_counts"]["air_time"] == 7
    assert len(result["examples"]["negative_air_time"]) == 5
    assert "2026-03-01" in result["daily_counts"]
    assert len(result["missing_dates"]) == 27


def test_missing_files_gaps_duplicates_and_date_overlap(tmp_path):
    """
    作用：验证跨文件重复、重叠日期、缺文件与未选缺月分别登记
    输入：pytest 临时目录
    输出：无，不创建缺失月份占位文件
    """
    path = write_month(tmp_path / "2026-01.csv", [synthetic_row("2026-01-01")])
    (tmp_path / "2026-03.csv").write_bytes(path.read_bytes())
    report = audit_months(tmp_path, ["2026-01", "2026-03", "2026-04"])
    assert report["missing_files"] == ["2026-04"]
    assert report["unselected_gap_months"] == ["2026-02"]
    assert report["duplicate_files"] == [["2026-01", "2026-03"]]
    assert report["overlapping_dates"][0]["dates"] == ["2026-01-01"]
    assert not report["technical_checks_passed"]
    assert not report["database_imported"]
    assert not report["provenance_verified"]
    assert not (tmp_path / "2026-04.csv").exists()
    assert "未导入数据库" in render_summary(report)


@pytest.mark.parametrize("content", [b"", b'FlightDate\n"unterminated', b"\xff\xfe"])
def test_empty_or_unreadable_input_is_not_success(tmp_path, content):
    """
    作用：验证空文件、CSV语法错误和编码错误不能冒充完整核验
    输入：临时目录与故意损坏的合成字节
    输出：无
    """
    path = tmp_path / "2026-02.csv"
    path.write_bytes(content)
    result = inspect_month(path, "2026-02")
    assert not result["technical_checks_passed"]
    assert result["source_unchanged"]
    if content:
        assert not result["scan_complete"]
        assert result["scan_error"] is not None


def test_source_changes_detected(tmp_path, monkeypatch):
    """
    作用：验证扫描期间哈希变化阻断稳定核验结论
    输入：临时目录和哈希替身
    输出：无
    """
    path = write_month(tmp_path / "2026-02.csv", [synthetic_row()])
    hashes = iter(["before", "after"])
    monkeypatch.setattr("metric_pilot.data.audit.checksum", lambda _: next(hashes))
    assert "SOURCE_CHANGED_DURING_SCAN" in inspect_month(path, "2026-02")["technical_blockers"]


def test_cli_preserves_outputs_and_writes_no_application_database(tmp_path):
    """
    作用：核验CLI只发布新审计目录，拒绝覆盖旧记录或写入原件目录
    输入：pytest 临时目录
    输出：无
    """
    source = tmp_path / "data"
    source.mkdir()
    write_month(source / "2026-02.csv", [synthetic_row(day) for day in month_days("2026-02")])
    output = tmp_path / "audit"
    command = [
        sys.executable,
        "-m",
        "metric_pilot.data.audit",
        "--input-dir",
        str(source),
        "--periods",
        "2026-02",
        "--output-dir",
        str(output),
    ]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    manifest = (output / "manifest.json").read_bytes()
    assert json.loads(manifest)["technical_checks_passed"]
    assert not list(tmp_path.rglob("*.duckdb"))
    assert not list(tmp_path.rglob("*.sqlite"))
    assert subprocess.run(command, cwd=ROOT, capture_output=True, timeout=30).returncode == 2
    assert (output / "manifest.json").read_bytes() == manifest
    command[-1] = str(source / "audit")
    assert subprocess.run(command, cwd=ROOT, capture_output=True, timeout=30).returncode == 2
    assert not (source / "audit").exists()


@pytest.mark.parametrize("period", ["2026-13", "2026-2", "../../bad", "0000-01"])
def test_invalid_period_rejected(period):
    """
    作用：拒绝非法月份与路径字符，正确使用实际日历
    输入：非法月份
    输出：无
    """
    with pytest.raises(ValueError):
        month_days(period)
    assert len(month_days("2028-02")) == 29


def test_duplicate_periods_rejected(tmp_path):
    """
    作用：拒绝重复选择月份避免重复计数
    输入：pytest 临时目录
    输出：无
    """
    with pytest.raises(ValueError, match="UNIQUE_PERIODS_REQUIRED"):
        audit_months(tmp_path, ["2026-02", "2026-02"])


def test_user_source_confirmation_is_not_independent_authentication(tmp_path):
    """
    作用：区分用户来源声明、下载时间未知和独立来源认证
    输入：pytest 临时目录
    输出：无
    """
    write_month(tmp_path / "2026-02.csv", [synthetic_row()])
    report = audit_months(tmp_path, ["2026-02"], user_confirmed_reporting_all=True)
    source = report["months"][0]["source"]
    assert source["table_basis"] == "user_statement"
    assert source["geographic_selection"] == "all"
    assert source["acquired_at"] is None
    assert source["download_receipt"] is None
    assert not report["provenance_verified"]
    assert "用户确认" in render_summary(report)


def test_independent_baseline_detects_tampered_counts(tmp_path, monkeypatch):
    """
    作用：验证独立原始列基线能发现清单行数错误，且不依赖导入解析
    输入：临时目录与脚本导入路径
    输出：无
    """
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from verify_bts_audit import verify
    from verify_bts_snapshot import RAW_COLUMNS

    # 转成官方导出表头形式，数据仍为明确合成的 ZZ 航班。
    by_internal = {name: original for original, name, *_ in FIELDS}
    rows = [synthetic_row(day) for day in month_days("2026-02")]
    path = tmp_path / "2026-02.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(RAW_COLUMNS)
        for day, row in enumerate(rows, start=1):
            row["FlightDate"] = f"2/{day}/2026 12:00:00 AM"
            writer.writerow([row[by_internal[internal]] for internal in RAW_COLUMNS.values()])
    report = audit_months(tmp_path, ["2026-02"])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(report), encoding="utf-8")
    assert verify(manifest, tmp_path)["passed"]
    report["months"][0]["counts"]["rows_raw"] += 1
    manifest.write_text(json.dumps(report), encoding="utf-8")
    result = verify(manifest, tmp_path)
    assert not result["passed"]
    assert not result["months"][0]["checks"]["rows_raw"]
