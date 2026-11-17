import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from metric_pilot.agent import models as m
from metric_pilot.agent.compaction import RunCompactor, commit_checkpoint, validate_summary
from metric_pilot.agent.context import fit_context, initial_messages, model_message_bytes
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.data.common import DataError
from sqlalchemy import select
from test_history_context import agent_workspace as agent_workspace
from test_history_context import registry_for
from test_investigations import enqueue, investigation

"""Run-local compaction validation must reject forged sources and preserve coverage."""


def test_summary_sources_and_structure():
    allowed = {"a": {"verification": "frozen_query_only"}}
    valid = {"background": "已查，仍须核实", "source_ids": ["a"], "gaps": ["未查天气"]}
    assert validate_summary(valid, allowed)["sources"] == allowed
    for candidate in [
        dict(valid, source_ids=["other"]),
        dict(valid, verified=True),
        dict(valid, background="x" * 10000),
    ]:
        with pytest.raises(DataError):
            validate_summary(candidate, allowed)


def test_checkpoint_rejects_cancel_and_stale_revision(agent_workspace):
    run = enqueue(agent_workspace, investigation(agent_workspace))
    agent_workspace[1].claim()
    engine = agent_workspace[1].engine
    cancel = threading.Event()
    commit_checkpoint(engine, run["id"], 0, {"test": "synthetic"}, cancel, time.monotonic() + 30)
    with pytest.raises(DataError):
        commit_checkpoint(engine, run["id"], 0, {}, cancel, time.monotonic() + 30)
    cancel.set()
    with pytest.raises(DataError):
        commit_checkpoint(engine, run["id"], 1, {}, cancel, time.monotonic() + 30)
    with engine.connect() as c:
        assert len(c.execute(select(m.context_checkpoints)).all()) == 1


@pytest.mark.parametrize("partial", [False, True])
def test_trigger_counts_deduplicated_context(partial):
    rows = [["v" * 500], ["w" * 500]]
    registry = SimpleNamespace(
        history_enabled=True,
        run={"id": "synthetic", "question": "test"},
        artifact_ids={"a"},
        resolved=set(),
        definitions={},
        prior_artifacts=[],
        working_summary=None,
        date_range=SimpleNamespace(model_dump=lambda: {}),
        query_summaries=[
            {
                "artifact_id": "a",
                "rows": 2,
                "truncated": False,
                "result_preview": {"complete": True, "columns": ["x"], "rows": rows},
            }
        ],
    )
    payload = {
        "ok": True,
        "data": {
            "artifact": {
                "id": "a",
                "returned_rows": 2,
                "truncated": False,
                "columns": [{"name": "x", "type": "VARCHAR"}],
            },
            "rows": rows[:1] if partial else rows,
            "offset": 0,
        },
    }
    groups = [
        [{"role": "assistant", "content": "earlier action"}],
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "t", "function": {"name": "read_artifact", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "t", "content": json.dumps(payload)},
        ],
    ]
    actual = fit_context([], groups, registry, 10**9)
    assert bool(json.loads(actual[1]["content"])["recent_successful_queries"]) == partial
    registry.run["budget"] = {
        "context_tokens": int((model_message_bytes(actual) + 500) / 0.8) + 3000 + 1024,
        "output_tokens": 3000,
    }

    async def unexpected(*args, **kwargs):
        raise AssertionError("input below threshold must not request summary")

    runtime = SimpleNamespace(provider=SimpleNamespace(next_summary=True), wait_model=unexpected)
    compactor = RunCompactor()
    asyncio.run(
        compactor.maybe(
            runtime,
            [],
            groups,
            registry,
            27000,
            [],
            SimpleNamespace(deadline_monotonic=time.monotonic() + 180),
            threading.Event(),
            {},
            False,
        )
    )
    assert compactor.attempts == 0


@pytest.mark.parametrize("failure", [None, "invalid", "write"])
def test_two_compactions_replace_summary_and_keep_contiguous_coverage(
    agent_workspace, monkeypatch, failure
):
    run = enqueue(agent_workspace, investigation(agent_workspace))
    agent_workspace[1].claim()
    agent_workspace[0].run_history_compaction = True
    registry = registry_for(agent_workspace, run)
    registry.ctx = SimpleNamespace(deadline_monotonic=time.monotonic() + 180)

    class Provider:
        count = 0

        async def next_summary(self, messages):
            self.count += 1
            return {
                "candidate": {
                    "background": f"摘要{self.count}",
                    "source_ids": ["forged"] if failure == "invalid" else [],
                    "gaps": ["未查"],
                },
                "usage": {"total_tokens": 10},
                "attempts": 1,
                "calls": [],
            }

    runtime = AgentRuntime(agent_workspace[1], agent_workspace[0], Provider())
    compactor = RunCompactor()
    groups = [[{"role": "assistant", "content": "已查合成记录" * 180}] for _ in range(6)]
    usage = {}
    header = initial_messages(run, registry, [])
    expected_before = model_message_bytes(fit_context(header, groups, registry, 10**9))
    cancel = threading.Event()
    if failure == "write":

        def reject(*args):
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("synthetic write failure")

        monkeypatch.setattr("metric_pilot.agent.compaction.commit_checkpoint", reject)

    async def execute():
        await compactor.maybe(
            runtime, header, groups, registry, 27000, [], registry.ctx, cancel, usage, False
        )
        if failure:
            assert compactor.disabled and compactor.revision == 0
            assert registry.working_summary is None and len(groups) == 6
            await compactor.maybe(
                runtime, header, groups, registry, 27000, [], registry.ctx, cancel, usage, False
            )
            assert runtime.provider.count == 1
            return
        assert compactor.revision == 1
        assert registry.working_summary["background"] == "摘要1"
        groups.extend([[{"role": "assistant", "content": "新增合成记录" * 180}] for _ in range(6)])
        await compactor.maybe(
            runtime, header, groups, registry, 27000, [], registry.ctx, cancel, usage, False
        )

    asyncio.run(execute())
    if failure:
        with runtime.store.engine.connect() as c:
            assert not c.execute(select(m.context_checkpoints)).all()
        return
    assert compactor.revision == 2
    assert registry.working_summary["background"] == "摘要2"
    assert usage["model_requests"] == 2
    with runtime.store.engine.connect() as c:
        rows = (
            c.execute(select(m.context_checkpoints).order_by(m.context_checkpoints.c.revision))
            .mappings()
            .all()
        )
    assert rows[0]["payload"]["covered_end"] == rows[1]["payload"]["covered_start"]
    assert rows[0]["payload"]["before"] == expected_before


def test_summary_transport_never_retries(monkeypatch):
    import httpx
    from metric_pilot.agent.provider import AgentProvider
    from test_agent_provider_failures import config, install_transport

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    install_transport(monkeypatch, handler)
    provider = AgentProvider(config())
    with pytest.raises(DataError):
        asyncio.run(provider.next_summary([{"role": "user", "content": "synthetic"}]))
    assert len(requests) == 1


def test_pair_validation_rejects_duplicate_call_ids():
    from metric_pilot.agent.compaction import paired

    assert not paired(
        [
            [
                {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "a"}]},
                {"role": "tool", "tool_call_id": "a"},
            ]
        ]
    )


def test_summary_request_uses_largest_fitting_contiguous_prefix():
    from metric_pilot.agent.compaction import summary_request
    from metric_pilot.agent.context import model_message_bytes

    groups = [[{"role": "assistant", "content": f"action{i} " + "x" * 1800}] for i in range(8)]
    keep, messages = summary_request({}, groups, None, 5500)
    assert 0 < keep < len(groups)
    assert model_message_bytes(messages) <= 5500
    import json

    data = json.loads(messages[1]["content"])
    assert len(data["covered_prefix"]) == keep
    assert "action0" in data["covered_prefix"][0]["content"]
    assert f"action{keep}" not in messages[1]["content"]
