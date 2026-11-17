import copy

import pytest
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.data.presentation import scope_context
from metric_pilot.reports import models as rm
from metric_pilot.reports.api import markdown
from metric_pilot.reports.materialize import content_hash
from sqlalchemy import select
from test_investigations import agent_workspace as agent_workspace
from test_investigations import first_report, investigation, service


@pytest.mark.parametrize("period,end", [("2026-12", "2027-01-01"), ("2024-02", "2024-03-01")])
def test_legacy_scope_uses_only_frozen_month(period, end):
    payload = {"data_snapshot": {"period": period}, "scope": "wrong date"}
    old = copy.deepcopy(payload)
    value = scope_context(payload)
    assert value["main_date_range"] == {"start": period + "-01", "end_exclusive": end}
    assert value["range_source"] == "legacy_single_month"
    assert payload == old


def test_unknown_scope_does_not_guess():
    assert scope_context({"data_snapshot": {"period": None}})["main_date_range"] is None


def test_frozen_range_wins_and_new_scope_is_server_owned():
    value = scope_context(
        {"date_range": {"start": "2026-01-01", "end_exclusive": "2026-05-01"}}, new=True
    )
    assert value["policy"] == "server_scope_v1"
    assert "2026-05-01" in value["main_label"]


def test_output_scope_and_legacy_read_keep_hashes(agent_workspace):
    inv = investigation(agent_workspace)
    run = first_report(agent_workspace, inv)
    store = InvestigationStore(agent_workspace[1].engine)
    finding = store.read(inv["id"])["findings"][0]
    evidence = store.evidence(inv["id"], finding["id"])
    reports = service(agent_workspace)
    report = reports.read(run["result"]["report_id"], run["result"]["revision_id"])
    assert (
        finding["scope_context"] == evidence["scope_context"] == report.scope_context.model_dump()
    )
    assert report.scope_context.main_label in markdown(report)
    assert report.scope_context.policy == "server_scope_v1"
    engine = agent_workspace[1].engine
    with engine.begin() as c:
        row = (
            c.execute(select(rm.revisions).where(rm.revisions.c.id == report.revision_id))
            .mappings()
            .one()
        )
        payload = copy.deepcopy(row["payload"])
        payload.pop("scope_context", None)
        payload.pop("date_range", None)
        digest = content_hash(payload)
        c.execute(
            rm.revisions.update()
            .where(rm.revisions.c.id == report.revision_id)
            .values(payload=payload, content_hash=digest)
        )
    restored = reports.read(report.report_id, report.revision_id)
    assert restored.scope_context.range_source == "legacy_single_month"
    assert restored.content_hash == digest
    assert reports.frozen(report.report_id, report.revision_id) == (payload, digest)
    assert restored.scope_context.main_label in markdown(restored)

    from metric_pilot.reports.contracts import ReportRevisionWrite

    revised = reports.revise(
        report.report_id,
        ReportRevisionWrite(expected_version=report.version, report=restored.draft),
    )
    assert revised.scope_context.policy == "server_scope_v1"
    assert revised.scope_context.main_date_range == restored.scope_context.main_date_range
    assert reports.frozen(report.report_id, report.revision_id) == (payload, digest)
