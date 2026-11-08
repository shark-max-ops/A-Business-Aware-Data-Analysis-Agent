"""用既有独立原始 CSV 基线复核阶段 6.A 清单；不连接应用数据库。"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from verify_bts_snapshot import checksum, profile_csv


def verify(manifest_path: Path, input_dir: Path) -> dict:
    """
    作用：以独立列映射、日期及 Decimal 解析重算清单中的主要统计
    输入：manifest_path - 核验清单；input_dir - 真实原件目录
    输出：实际比较项和失败记录，不将缺失来源凭据视为已证实
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = []
    for month in manifest["months"]:
        period = month["period"]
        path = input_dir / (period + ".csv")
        before = checksum(path)
        baseline = profile_csv(path, period)
        after = checksum(path)
        counts = month["counts"]
        checks = {
            "stable_source": before == after == month["sha256_before"] == month["sha256_after"],
            "bytes": path.stat().st_size == month["bytes"],
            "rows_raw": baseline["raw_rows"] == counts["rows_raw"],
            "date_min": baseline["date_min"] == month["date_min"],
            "date_max": baseline["date_max"] == month["date_max"],
            "days": baseline["days"] == len(month["daily_counts"]),
            "out_of_period": baseline["raw_rows"] - baseline["exposed_rows"]
            == counts["out_of_period"],
            "null_counts": baseline["null_counts"] == month["null_counts"],
            "negative_duration_counts": baseline["negative_duration_counts"]
            == month["negative_duration_counts"],
            "threshold_mismatches": baseline["threshold_mismatches"]
            == counts["arrival_threshold_mismatches"],
            "nonnegative_mapping_mismatches": baseline["nonnegative_mapping_mismatches"]
            == counts["nonnegative_delay_mismatches"],
            "cancelled_diverted_overlap": baseline["cancelled_diverted_overlap"]
            == counts["cancelled_diverted_overlap"],
            "negative_arrivals": baseline["negative_arrivals"] == counts["negative_arrivals"],
            "cancelled": sum(c["cancelled"] for c in baseline["carriers"].values())
            == month["flag_counts"]["cancelled"].get("1", 0),
            "diverted": sum(c["diverted"] for c in baseline["carriers"].values())
            == month["flag_counts"]["diverted"].get("1", 0),
        }
        results.append({"period": period, "checks": checks})
        print(f"{period}: {sum(checks.values())}/{len(checks)} independent comparisons", flush=True)
    return {
        "verified_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "method": "verify_bts_snapshot.profile_csv: independent raw columns/datetime/Decimal",
        "months": results,
        "passed": bool(results) and all(all(r["checks"].values()) for r in results),
        "scope": "仅主要统计交叉核对；重复计数另由人工可算合成样本验证；不核实下载来源。",
    }


def main() -> None:
    """
    作用：执行独立复核并独占保存本地验收结果
    输入：命令行清单、原件目录及尚不存在的结果文件路径
    输出：验收 JSON，比较失败退出1
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input-dir", type=Path, default=Path("data/bts"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # 预先检查并最终使用独占写入，避免覆盖清单、原件或旧验收。
    if args.output.exists():
        parser.error("OUTPUT_ALREADY_EXISTS")
    result = verify(args.manifest, args.input_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
