"""通过正式 API 复测指定失败问题，保留旧运行，不重置调查额度。"""

import argparse
import json
import time
from urllib.parse import urlsplit

from metric_pilot.data.common import utc_now
from metric_pilot.settings import Settings
from verify_agent import Client
from verify_reports import values


def verify(original_id):
    """
    作用：原调查中重新提交实际失败问题并核对报告、事件及历史保留
    输入：原失败运行标识
    输出：是否完成真实验收
    """
    settings = Settings()
    target = settings.storage_dir / "validation" / "stage4-user-retry.json"
    record = {
        "created_at": utc_now(),
        "original_run_id": original_id,
        "passed": False,
        "scope": "execution_and_frozen_values_not_semantic_accuracy",
    }
    api = Client("http://127.0.0.1:8000/api/v1")
    try:
        # 只沿用用户已明确授权的服务，不因本机配置变化扩展发送目的地。
        if urlsplit(settings.llm_base_url).hostname != "api.deepseek.com":
            raise RuntimeError("LIVE_DESTINATION_CHANGED")
        schema = api.client.get("http://127.0.0.1:8000/openapi.json").json()
        if "error_details" not in schema["components"]["schemas"]["RunDTO"]["properties"]:
            raise RuntimeError("BACKEND_RESTART_REQUIRED")
        original = api.request("GET", f"/runs/{original_id}")
        if original["status"] != "failed" or not original.get("investigation_id"):
            raise RuntimeError("ORIGINAL_FAILED_INVESTIGATION_REQUIRED")
        inv_id = original["investigation_id"]
        before = api.request("GET", f"/investigations/{inv_id}")
        if before["active_run_id"]:
            raise RuntimeError("INVESTIGATION_BUSY")
        created = api.request(
            "POST",
            "/runs",
            json={
                "conversation_id": original["conversation_id"],
                "question": original["question"],
                "snapshot_id": original["snapshot_id"],
                "metric_revision_ids": original["metric_revision_ids"],
                "investigation_id": inv_id,
                "expected_investigation_version": before["version"],
            },
        )
        record.update(investigation_id=inv_id, run=created)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        end = time.monotonic() + created["budget"]["timeout_seconds"] + 30
        run = created
        while run["status"] in {"queued", "running"}:
            if time.monotonic() >= end:
                api.request("POST", f"/runs/{run['id']}/cancel")
                raise RuntimeError("VALIDATION_WAIT_TIMEOUT")
            time.sleep(0.3)
            run = api.request("GET", f"/runs/{run['id']}")
        record["run"] = run
        events, after = [], 0
        while True:
            page = api.request("GET", f"/runs/{run['id']}/event-log?after={after}")
            events.extend(page["items"])
            if not page["next_cursor"]:
                break
            after = int(page["next_cursor"])
        record["events"] = events
        if run["status"] != "completed" or run["result"]["kind"] != "report":
            raise RuntimeError(run.get("error_code") or "REPORT_REQUIRED")
        if run["knowledge_snapshot_id"] != original["knowledge_snapshot_id"]:
            raise RuntimeError("KNOWLEDGE_SNAPSHOT_CHANGED")
        if [event["seq"] for event in events] != list(range(1, run["seq"] + 1)):
            raise RuntimeError("EVENT_REPLAY_MISMATCH")
        starts = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.started"]
        ends = [e["payload"]["tool_execution_id"] for e in events if e["type"] == "tool.finished"]
        if starts != ends or len(set(starts)) != len(starts):
            raise RuntimeError("TOOL_PAIRING_MISMATCH")
        result = run["result"]
        report = api.request(
            "GET", f"/reports/{result['report_id']}/revisions/{result['revision_id']}"
        )
        record["report"] = report
        pages, checked = {}, 0
        for block in report["blocks"]:
            for value in values(block):
                ref = value["value_ref"]
                key = (ref["artifact_id"], ref["row"])
                if key not in pages:
                    pages[key] = api.request(
                        "GET", f"/runs/{run['id']}/artifacts/{key[0]}?offset={key[1]}&limit=1"
                    )
                page = pages[key]
                names = [column["name"] for column in page["artifact"]["columns"]]
                if (
                    page["artifact"]["truncated"]
                    or page["rows"][0][names.index(ref["column"])] != value["raw"]
                ):
                    raise RuntimeError("FROZEN_VALUE_MISMATCH")
                checked += 1
        current = api.request("GET", f"/investigations/{inv_id}")
        old = api.request("GET", f"/runs/{original_id}")
        if old["status"] != "failed" or not {f["id"] for f in before["findings"]} <= {
            f["id"] for f in current["findings"]
        }:
            raise RuntimeError("ORIGINAL_HISTORY_CHANGED")
        if current["allocated"]["runs"] != before["allocated"]["runs"] + 1:
            raise RuntimeError("INVESTIGATION_ALLOCATION_MISMATCH")
        record.update(passed=True, checked_values=checked, investigation=current)
    except Exception as error:
        record["error_code"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
    finally:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(record, ensure_ascii=False, indent=2)
        target.write_text(content, encoding="utf-8")
        target.with_name(
            "stage4-user-retry-" + record["created_at"].replace(":", "-") + ".json"
        ).write_text(content, encoding="utf-8")
        api.client.close()
        print(
            json.dumps(
                {
                    "passed": record["passed"],
                    "error_code": record.get("error_code"),
                    "run_id": record.get("run", {}).get("id"),
                }
            ),
            flush=True,
        )
    return record["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    raise SystemExit(0 if verify(parser.parse_args().run_id) else 1)
