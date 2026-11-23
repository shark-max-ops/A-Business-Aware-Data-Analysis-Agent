"""从指定验收目录只读核对阶段9真实比较首段、零计数及同调查旧证据。"""

import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from metric_pilot.reports.api import escape_markdown, markdown
from metric_pilot.reports.contracts import ReportDTO


def verify(directory, original):
    """用原始 BTS 行独立复算每个对象的分母与取消数，逐份校验报告及导出。"""
    totals, cancelled = Counter(), Counter()
    with original.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            day = datetime.strptime(row["FL_DATE"].split()[0], "%m/%d/%Y").date()
            if day == date(2026, 1, 2):
                code = row["OP_UNIQUE_CARRIER"]
                totals[code] += 1
                cancelled[code] += int(Decimal(row["CANCELLED"]))
    assert totals["AA"] and totals["DL"] and not totals["ZZ"]
    previous = None
    for name in ("both", "missing", "followup"):
        case = json.loads((directory / f"{name}.json").read_text(encoding="utf8"))
        report = case["report"]
        block = next(b for b in report["blocks"] if b["id"] == report["answer_block_id"])
        assert block["verification"] == "checked_relation", name
        proof = block["proof"]
        check = proof["comparison"]
        expected = {"DL", "AA" if name == "both" else "ZZ"}
        assert set(check["expected"]) == expected
        record = case["artifacts"][check["artifact_id"]]
        meta = record["artifact"]
        assert meta["query_purpose"] == "reviewed_metric" and meta["metric_revision_ids"]
        columns = [c["name"] for c in meta["columns"]]
        present = {row[columns.index(check["key"])] for row in record["rows"]}
        assert present & expected == {code for code in expected if totals[code]}
        for row in record["rows"]:
            code = row[columns.index(check["key"])]
            if code in expected:
                assert row[columns.index(check["denominator"])] == totals[code]
                assert row[columns.index(check["numerator"])] == cancelled[code]
        text = "".join(s["text"] for s in block["segments"])
        exported = markdown(ReportDTO.model_validate(report))
        answer_lines = exported.splitlines()
        assert answer_lines[2:6] == [
            "## 已核对的结果集关系",
            "",
            "## " + escape_markdown(block["title"]),
            "",
        ]
        assert answer_lines[6] == escape_markdown(text)
        if name == "both":
            assert "DL 的比例更高" in text
        else:
            assert proof["confirmed_zero"] == ["ZZ"] and "无法比较" in text
            zero = case["artifacts"][check["zero_count_artifact_id"]]
            assert zero["rows"] == [[0]] and zero["artifact"]["id"] in block["evidence_ids"]
            if name == "followup":
                assert previous == (case["run"]["investigation_id"], set(block["evidence_ids"]))
            previous = case["run"]["investigation_id"], set(block["evidence_ids"])
        print(name, text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--csv", type=Path, default=Path("data/bts/2026-01.csv"))
    args = parser.parse_args()
    verify(args.directory, args.csv)
