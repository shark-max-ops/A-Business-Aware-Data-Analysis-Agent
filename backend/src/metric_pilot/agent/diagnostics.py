"""Allowlisted transport telemetry. Never project response bodies or exception text."""

import math


def safe_diagnostics(details):
    result = {"failure_kind": "unknown", "usage": {}}
    codes = {
        "COMPACT_INVALID_STRUCTURE",
        "COMPACT_INVALID_SOURCE",
        "COMPACT_SUMMARY_TOO_LARGE",
        "COMPACT_NO_BENEFIT",
        "COMPACT_COMMIT_FAILED",
        "COMPACT_COMMIT_REJECTED",
        "MODEL_HTTP_ERROR",
        "MODEL_REQUEST_FAILED",
        "MODEL_OUTPUT_INCOMPLETE",
        "MODEL_RESPONSE_INVALID",
        "MODEL_RESPONSE_TOO_LARGE",
        "MODEL_PROTOCOL_ERROR",
        "RUN_CANCELLED",
        "RUN_TIMEOUT",
        "COMPACT_LOCAL_TIMEOUT",
    }
    if details.get("error_code") in codes:
        result["error_code"] = details["error_code"]
    if type(details.get("status")) is int and 100 <= details["status"] <= 599:
        result["status"] = details["status"]
        result["failure_kind"] = "http_status"
    if details.get("failure_kind") in {
        "timeout",
        "connection",
        "connection_interrupted",
        "request_error",
        "invalid_json",
    }:
        result["failure_kind"] = details["failure_kind"]
    for key in ("attempts", "elapsed"):
        value = details.get(key)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result[key] = value
    usage = details.get("usage", {})
    if isinstance(usage, dict):
        result["usage"] = {
            k: v
            for k, v in usage.items()
            if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and type(v) is int
            and v >= 0
        }
    return result
