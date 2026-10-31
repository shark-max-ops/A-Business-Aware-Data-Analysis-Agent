"""真实模型工具调用验收；不审核或发布实际规则。"""

import argparse
import json
import time

import httpx
from metric_pilot.data.common import utc_now
from metric_pilot.settings import Settings


class Client:
    """
    作用：以真实 HTTP 驱动本地运行
    输入：服务地址
    输出：验收客户端
    """

    def __init__(self, base):
        """
        作用：绑定本地服务
        输入：基础地址
        输出：无
        """
        self.client = httpx.Client(base_url=base, timeout=20, trust_env=False)

    def request(self, method, path, **kwargs):
        """
        作用：读取 API 或给出安全错误
        输入：请求
        输出：JSON
        """
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(response.json().get("error", {}).get("code", "API_FAILED"))
        return response.json()

    def run(self, conversation_id, snapshot_id, question):
        """
        作用：等待真实运行结束
        输入：问题及固定来源
        输出：运行和事件
        """
        row = self.request(
            "POST",
            "/runs",
            json={
                "conversation_id": conversation_id,
                "snapshot_id": snapshot_id,
                "question": question,
            },
        )
        end = time.monotonic() + 210
        while row["status"] in {"queued", "running"}:
            if time.monotonic() > end:
                self.request("POST", f"/runs/{row['id']}/cancel")
                raise RuntimeError("VALIDATION_WAIT_TIMEOUT")
            time.sleep(0.25)
            row = self.request("GET", f"/runs/{row['id']}")
        events, after = [], 0
        while True:
            page = self.request("GET", f"/runs/{row['id']}/event-log?after={after}")
            events.extend(page["items"])
            if not page["next_cursor"]:
                break
            after = int(page["next_cursor"])
        return row, events


def verify(base, reuse_record=False):
    """
    作用：记录实际 Agent 查询及缺审核规则行为
    输入：本地服务
    输出：是否通过
    """
    api = Client(base)
    target = Settings().storage_dir / "validation" / "stage3-agent.json"
    existing = json.loads(target.read_text(encoding="utf-8")) if reuse_record else None
    report = {
        "created_at": utc_now(),
        "cases": [],
        "scope": "stage3_not_report_or_accuracy_benchmark",
        "reused_existing_model_runs": reuse_record,
        "passed": False,
    }
    try:
        sources = api.request("GET", "/sources")["items"]
        source = next(
            s for s in sources if s["source_kind"] == "official_bts" and s["current_snapshot_id"]
        )
        snapshots = api.request("GET", f"/sources/{source['id']}/snapshots")["items"]
        snapshot = next(s for s in snapshots if s["id"] == source["current_snapshot_id"])
        report["snapshot_id"] = snapshot["id"]
        conversation = api.request("POST", "/conversations") if not reuse_record else None
        cases = [
            ("basic_query", "我想核对数据范围：这个月的数据有多少条记录，覆盖哪些日期？"),
            ("metric_unreviewed", "这个月哪家航空公司的延误率最高？"),
            ("missing_data", "根据这些航班数据，哪家航空公司的利润最高？"),
        ]
        for name, question in cases:
            if reuse_record:
                old = next(case for case in existing["cases"] if case["name"] == name)
                run = api.request("GET", f"/runs/{old['run']['id']}")
                if run["snapshot_id"] != snapshot["id"] or run["question"] != question:
                    raise RuntimeError("REUSE_INPUT_MISMATCH")
                events = api.request("GET", f"/runs/{run['id']}/event-log")["items"]
                if len(events) == 100:
                    raise RuntimeError("REUSE_EVENT_PAGINATION_REQUIRED")
            else:
                run, events = api.run(conversation["id"], snapshot["id"], question)
            case = {
                "name": name,
                "run": run,
                "tools": [e["payload"] for e in events if e["type"] == "tool.finished"],
            }
            if name == "basic_query":
                pages = [
                    api.request("GET", f"/runs/{run['id']}/artifacts/{ident}")
                    for ident in (run.get("result") or {}).get("artifact_ids", [])
                ]
                case["artifacts"] = pages
                values = [v for page in pages for row in page["rows"] for v in row]
                case["passed"] = (
                    run["status"] == "completed"
                    and run["result"]["kind"] == "query_result"
                    and snapshot["row_count"] in values
                    and "2026-01-01" in json.dumps(values)
                    and "2026-01-31" in json.dumps(values)
                    and any(t["tool"] == "query_sql" and t["ok"] for t in case["tools"])
                    and any(t["tool"] == "describe_table" and t["ok"] for t in case["tools"])
                )
            else:
                case["passed"] = (
                    run["status"] == "completed"
                    and (run.get("result") or {}).get("kind") == "clarification"
                    and not any(t.get("artifact_ids") for t in case["tools"])
                )
            if name == "metric_unreviewed":
                case["passed"] = case["passed"] and any(
                    t["tool"] == "resolve_metric" and not t["ok"] for t in case["tools"]
                )
            report["cases"].append(case)
            print(
                json.dumps(
                    {
                        "case": name,
                        "status": run["status"],
                        "error_code": run["error_code"],
                        "tools": [t["tool"] for t in case["tools"]],
                        "passed": case["passed"],
                    }
                )
            )
        report["passed"] = all(case["passed"] for case in report["cases"])
        return report["passed"]
    finally:
        target = Settings().storage_dir / "validation" / "stage3-agent.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        historical = target.with_name(
            "stage3-agent-" + report["created_at"].replace(":", "-") + ".json"
        )
        historical.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        api.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument(
        "--reuse-record", action="store_true", help="仅重新读取已有运行与冻结结果核对，不调用模型"
    )
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.base_url, args.reuse_record) else 1)
