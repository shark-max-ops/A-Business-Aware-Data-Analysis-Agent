import copy
import json
import time

from metric_pilot.agent.context import model_message_bytes
from metric_pilot.agent.diagnostics import safe_diagnostics
from metric_pilot.agent.provider import AgentProvider


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def totals(records):
    result = {"model_requests": sum(r.get("attempts", 0) for r in records), "usage_complete": True}
    for r in records:
        usage = r.get("usage", {})
        if not all(k in usage for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
            result["usage_complete"] = False
        for k, v in usage.items():
            result[k] = result.get(k, 0) + v
    return result


class Trace(AgentProvider):
    def __init__(self, config, folder):
        super().__init__(config)
        self.folder = folder
        self.records = []

    async def next_turn(self, messages, tools, **kwargs):
        kwargs["max_attempts"] = 1
        record = {
            "type": "summary" if kwargs.get("summary") else "business",
            "input_units": model_message_bytes(messages),
            "schema_units": len(
                json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode()
            ),
        }
        self.records.append(record)
        started = time.monotonic()
        # Input audit is separate; format repair final body stays only in memory.
        visible = copy.deepcopy(messages)
        if kwargs.get("summary") and len(visible) > 2:
            visible[2] = {"role": "assistant", "content": "[memory-only repair body]"}
        save(self.folder / ("input-" + str(len(self.records)) + ".json"), visible)
        try:
            turn = await super().next_turn(messages, tools, **kwargs)
            record.update(safe_diagnostics(turn))
            return turn
        except BaseException as error:
            record.update(
                safe_diagnostics(
                    {"error_code": getattr(error, "code", None), **getattr(error, "details", {})}
                )
            )
            record["attempts"] = self.current_attempts
            raise
        finally:
            record["elapsed"] = time.monotonic() - started
            save(self.folder / "requests.json", self.records)
