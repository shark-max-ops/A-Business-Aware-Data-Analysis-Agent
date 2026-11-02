"""在独立验收调查中复跑用户原题，不重置原调查预算，不审核指标。"""

import argparse
import json
import time
from urllib.parse import urlsplit

from metric_pilot.data.common import utc_now
from metric_pilot.settings import Settings
from verify_agent import Client
from verify_reports import values


def verify(original_id, resume=False):
    """
    作用：验证真实模型能完成原始分布探索并保留未审核身份及真实引用
    输入：用户原失败运行 ID，仅用于读取原问题与快照
    输出：是否通过执行及引用检查，不代表业务语义准确率
    """
    config = Settings()
    target = config.storage_dir / "validation" / "stage4-exploration.json"
    record = {
        "created_at": utc_now(),
        "original_run_id": original_id,
        "passed": False,
        "scope": "execution_and_exploration_identity_not_semantic_accuracy",
    }
    api = Client("http://127.0.0.1:8000/api/v1")
    try:
        if urlsplit(config.llm_base_url).hostname != "api.deepseek.com":
            raise RuntimeError("LIVE_DESTINATION_CHANGED")
        original = api.request("GET", f"/runs/{original_id}")
        before = api.request("GET", f"/investigations/{original['investigation_id']}")
        if resume:
            previous = json.loads(target.read_text(encoding="utf-8"))
            if (
                not previous.get("independent_validation")
                or previous["original_run_id"] != original_id
            ):
                raise RuntimeError("VALIDATION_RESUME_MISMATCH")
            inv = api.request("GET", f"/investigations/{previous['investigation_id']}")
            conversation = {"id": inv["conversation_id"]}
        else:
            conversation = api.request("POST", "/conversations")
            inv = api.request(
                "POST",
                "/investigations",
                json={
                    "conversation_id": conversation["id"],
                    "goal": original["question"],
                    "scope": "独立修复验收：原始字段、质量与分布探索；"
                    "不重置原调查额度，不批准正式指标。",
                    "snapshot_id": original["snapshot_id"],
                },
            )
        record.update(independent_validation=True, investigation_id=inv["id"])
        run = api.request(
            "POST",
            "/runs",
            json={
                "conversation_id": conversation["id"],
                "question": original["question"],
                "snapshot_id": original["snapshot_id"],
                "investigation_id": inv["id"],
                "expected_investigation_version": inv["version"],
            },
        )
        record["run"] = run
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
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
            raise RuntimeError(run.get("error_code") or "REPORT_REQUIRED")
        if [event["seq"] for event in events] != list(range(1, run["seq"] + 1)):
            raise RuntimeError("EVENT_REPLAY_MISMATCH")
        starts = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.started"]
        ends = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.finished"]
        if starts != ends or len(set(starts)) != len(starts):
            raise RuntimeError("TOOL_PAIRING_MISMATCH")
        queries = [
            e["payload"]["data"]["artifact"]
            for e in events
            if e["type"] == "tool.finished" and e["payload"].get("artifact_ids")
        ]
        if not queries or any(
            a["query_purpose"] != "exploratory"
            or a["metric_revision_ids"]
            or not a["exploration_scope"]
            for a in queries
        ):
            raise RuntimeError("EXPLORATION_IDENTITY_REQUIRED")
        result = run["result"]
        report = api.request(
            "GET", f"/reports/{result['report_id']}/revisions/{result['revision_id']}"
        )
        record["report"] = report
        if report["metrics"] or not any("包含数据探索结果" in s for s in report["limitations"]):
            raise RuntimeError("REPORT_EXPLORATION_IDENTITY_REQUIRED")
        checked = 0
        for block in report["blocks"]:
            for value in values(block):
                ref = value["value_ref"]
                page = api.request(
                    "GET",
                    f"/runs/{run['id']}/artifacts/{ref['artifact_id']}?offset={ref['row']}&limit=1",
                )
                names = [c["name"] for c in page["artifact"]["columns"]]
                if page["rows"][0][names.index(ref["column"])] != value["raw"]:
                    raise RuntimeError("FROZEN_VALUE_MISMATCH")
                checked += 1
        after = api.request("GET", f"/investigations/{original['investigation_id']}")
        if before["allocated"] != after["allocated"] or before["run_ids"] != after["run_ids"]:
            raise RuntimeError("ORIGINAL_INVESTIGATION_CHANGED")
        findings = api.request("GET", f"/investigations/{inv['id']}")["findings"]
        if not findings:
            raise RuntimeError("FINDING_REQUIRED")
        record.update(passed=True, checked_values=checked, findings=findings)
    except Exception as exc:
        record["error_code"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(record, ensure_ascii=False, indent=2)
        target.write_text(content, encoding="utf-8")
        target.with_name(
            "stage4-exploration-" + record["created_at"].replace(":", "-") + ".json"
        ).write_text(content, encoding="utf-8")
        api.client.close()
        print(
            json.dumps(
                {
                    "passed": record["passed"],
                    "error_code": record.get("error_code"),
                    "run_id": record.get("run", {}).get("id"),
                }
            )
        )
    return record["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.run_id, args.resume) else 1)
