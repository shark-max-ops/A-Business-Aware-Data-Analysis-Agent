"""Run-local repair preservation; captured blocks never bypass report validation."""

import copy
import json

from pydantic import ValidationError

from metric_pilot.agent.investigations import read_evidence_artifact
from metric_pilot.data.common import DataError
from metric_pilot.reports.contracts import ReportDraft
from metric_pilot.reports.materialize import artifact_ids


def safe_structure(value):
    if isinstance(value, dict):
        return {
            k: safe_structure(v)
            for k, v in value.items()
            if k
            not in {"thought", "thoughts", "reasoning", "reasoning_content", "chain_of_thought"}
        }
    if isinstance(value, list):
        return [safe_structure(v) for v in value]
    return value


class RepairGuard:
    def __init__(self):
        self.raw = None
        self.blocks = []
        self.required = []
        self.fields = {}

    def capture(self, payload, service, run):
        if self.raw is not None or not isinstance(payload, dict):
            return
        report = payload.get("report")
        if not isinstance(report, dict) or not isinstance(report.get("blocks"), list):
            return
        # Only recognized report fields are projected; extension/thought fields are never retained.
        report = {
            k: safe_structure(copy.deepcopy(v))
            for k, v in report.items()
            if k in ReportDraft.model_fields
        }
        if len(json.dumps(report, ensure_ascii=False).encode()) > 16384:
            return
        self.raw = {"kind": "report", "report": report}
        ids = [b.get("id") for b in report["blocks"] if isinstance(b, dict)]
        if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            return
        self.required = ids
        for key in ("title", "scope", "limitations"):
            value = report.get(key)
            if (
                key != "limitations"
                and isinstance(value, str)
                and value.strip()
                and len(value) <= (160 if key == "title" else 1000)
            ) or (
                key == "limitations"
                and isinstance(value, list)
                and len(value) <= 12
                and all(isinstance(i, str) for i in value)
            ):
                self.fields[key] = value
        for block in report["blocks"]:
            try:
                draft = self.shell([block])
                typed = ReportDraft.model_validate(draft)
                metrics = set()
                for ident in artifact_ids(typed, require_query=False):
                    metrics.update(
                        read_evidence_artifact(service.query, run, ident, 0, 1)["artifact"][
                            "metric_revision_ids"
                        ]
                    )
                draft["metric_revision_ids"] = sorted(metrics)
                service.prepare(run, draft, require_query=False)
                self.blocks.append(copy.deepcopy(block))
            except (DataError, ValidationError, TypeError, KeyError):
                continue

    def shell(self, blocks):
        return {
            "schema_version": "1",
            "title": self.fields.get("title", "Retained results"),
            "scope": self.fields.get("scope", "Fixed run scope"),
            "limitations": self.fields.get("limitations", []),
            "blocks": blocks,
            "metric_revision_ids": [],
        }

    def check(self, report):
        current = report.get("blocks", [])
        ids = [b.get("id") for b in current]
        protected = [b["id"] for b in self.blocks]
        if (
            any(i not in ids for i in self.required)
            or [i for i in ids if i in protected] != protected
            or any(b not in current for b in self.blocks)
            or any(report.get(k) != v for k, v in self.fields.items())
        ):
            raise DataError("REPORT_REPAIR_CONTENT_LOSS")

    def fallback(self, service, run, warnings):
        if not self.blocks:
            return None
        draft = self.shell(self.blocks)
        metrics = set()
        for ident in artifact_ids(ReportDraft.model_validate(draft), require_query=False):
            metrics.update(
                read_evidence_artifact(service.query, run, ident, 0, 1)["artifact"][
                    "metric_revision_ids"
                ]
            )
        draft["metric_revision_ids"] = sorted(metrics)
        primary_id = self.raw["report"].get("primary_comparison_finding_id")
        if primary_id:
            draft["primary_comparison_finding_id"] = primary_id
        omitted = [i for i in self.required if i not in {b["id"] for b in self.blocks}]
        prepared = service.prepare(
            run,
            draft,
            require_query=False,
            retained_report=True,
            runtime_warnings=[
                *warnings,
                "Report repair incomplete; retained original validated blocks; omitted: "
                + ", ".join(omitted),
            ],
        )
        prepared["partial"] = True
        return prepared
