"""在明确的新分支中真实重建基线并继续复核；不修改已耗尽的原调查额度。"""

import argparse
import json
import time
from urllib.parse import urlsplit

from metric_pilot.data.common import utc_now
from metric_pilot.settings import Settings
from verify_agent import Client
from verify_reports import values


def finish_case(api, investigation, original):
    """
    作用：按原题运行并核对报告引用及发现，保留真实失败状态
    输入：本机 API、验收分支及原问题记录
    输出：案例原件
    """
    run = api.request(
        "POST",
        "/runs",
        json={
            "conversation_id": investigation["conversation_id"],
            "question": original["question"],
            "snapshot_id": original["snapshot_id"],
            "investigation_id": investigation["id"],
            "expected_investigation_version": investigation["version"],
        },
    )
    record = {"original_run_id": original["id"], "run": run}
    deadline = time.monotonic() + run["budget"]["timeout_seconds"] + 30
    while run["status"] in {"queued", "running"}:
        if time.monotonic() >= deadline:
            api.request("POST", f"/runs/{run['id']}/cancel")
            raise RuntimeError("VALIDATION_WAIT_TIMEOUT")
        time.sleep(0.3)
        run = api.request("GET", f"/runs/{run['id']}")
    record["run"] = run
    events = api.request("GET", f"/runs/{run['id']}/event-log")["items"]
    record["events"] = events
    if run["status"] != "completed" or run["result"]["kind"] != "report":
        return record
    if [event["seq"] for event in events] != list(range(1, run["seq"] + 1)):
        raise RuntimeError("EVENT_REPLAY_MISMATCH")
    starts = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.started"]
    ends = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.finished"]
    if starts != ends:
        raise RuntimeError("TOOL_PAIRING_MISMATCH")
    result = run["result"]
    report = api.request("GET", f"/reports/{result['report_id']}/revisions/{result['revision_id']}")
    record["report"] = report
    checked = 0
    for block in report["blocks"]:
        for value in values(block):
            ref = value["value_ref"]
            page = api.request(
                "GET",
                f"/runs/{run['id']}/artifacts/{ref['artifact_id']}?offset={ref['row']}&limit=1",
            )
            names = [column["name"] for column in page["artifact"]["columns"]]
            if page["rows"][0][names.index(ref["column"])] != value["raw"]:
                raise RuntimeError("FROZEN_VALUE_MISMATCH")
            checked += 1
    inv = api.request("GET", f"/investigations/{investigation['id']}")
    record["findings"] = [f for f in inv["findings"] if f["run_id"] == run["id"]]
    if not record["findings"]:
        raise RuntimeError("FINDING_REQUIRED")
    record.update(passed=True, checked_values=checked)
    return record


def verify(baseline_id, followup_id):
    """
    作用：在显式验收分支中依次运行原始两题，不扩容原调查
    输入：真实基线和失败追问 ID
    输出：执行及引用验收结果，不代表整体语义准确率
    """
    config = Settings()
    target = config.storage_dir / "validation" / "stage4-history-followup.json"
    record = {
        "created_at": utc_now(),
        "passed": False,
        "cases": [],
        "scope": "execution_and_frozen_values_not_accuracy_benchmark",
    }
    api = Client("http://127.0.0.1:8000/api/v1")
    try:
        if urlsplit(config.llm_base_url).hostname != "api.deepseek.com":
            raise RuntimeError("LIVE_DESTINATION_CHANGED")
        baseline = api.request("GET", f"/runs/{baseline_id}")
        followup = api.request("GET", f"/runs/{followup_id}")
        parent = api.request("GET", f"/investigations/{followup['investigation_id']}")
        if baseline["investigation_id"] != parent["id"]:
            raise RuntimeError("ORIGINAL_INVESTIGATION_MISMATCH")
        branch = api.request(
            "POST",
            "/investigations",
            json={
                "conversation_id": parent["conversation_id"],
                "goal": parent["goal"],
                "scope": "修复验收分支：固定原快照重新建立基线，再复核历史概括；不修改原调查额度。",
                "snapshot_id": parent["snapshot_id"],
                "parent_id": parent["id"],
            },
        )
        record.update(parent_id=parent["id"], investigation_id=branch["id"])
        for original in (baseline, followup):
            case = finish_case(api, branch, original)
            record["cases"].append(case)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "run_id": case["run"]["id"],
                        "status": case["run"]["status"],
                        "error_code": case["run"].get("error_code"),
                    }
                ),
                flush=True,
            )
            if not case.get("passed"):
                raise RuntimeError(case["run"].get("error_code") or "REPORT_REQUIRED")
            branch = api.request("GET", f"/investigations/{branch['id']}")
        if (
            record["cases"][0]["run"]["knowledge_snapshot_id"]
            != record["cases"][1]["run"]["knowledge_snapshot_id"]
        ):
            raise RuntimeError("KNOWLEDGE_SNAPSHOT_CHANGED")
        unchanged = api.request("GET", f"/investigations/{parent['id']}")
        if (
            parent["allocated"] != unchanged["allocated"]
            or parent["run_ids"] != unchanged["run_ids"]
        ):
            raise RuntimeError("ORIGINAL_INVESTIGATION_CHANGED")
        record.update(passed=True, investigation=branch)
    except Exception as exc:
        record["error_code"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(record, ensure_ascii=False, indent=2)
        target.write_text(content, encoding="utf-8")
        target.with_name(
            "stage4-history-followup-" + record["created_at"].replace(":", "-") + ".json"
        ).write_text(content, encoding="utf-8")
        api.client.close()
        print(json.dumps({"passed": record["passed"], "error_code": record.get("error_code")}))
    return record["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--followup-run-id", required=True)
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.baseline_run_id, args.followup_run_id) else 1)
