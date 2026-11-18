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


def test_number_free_prompt_example_and_repair_share_system():
    from metric_pilot.agent.compaction import SUMMARY_PROMPT, request_summary, summary_request

    _, messages = summary_request({}, [[{"role": "assistant", "content": "history"}]], None, 20000)
    assert messages[0]["content"] == SUMMARY_PROMPT
    assert "四、不复制数字" in SUMMARY_PROMPT
    assert "行号和行区间" in SUMMARY_PROMPT
    assert "q_example_month" in SUMMARY_PROMPT
    calls = []

    class Runtime:
        async def wait_model(self, request, *args, **kwargs):
            calls.append(request)
            return {
                "candidate": None
                if len(calls) == 1
                else {"background": "已生成统计产物", "source_ids": [], "gaps": []},
                "summary_body": "invalid",
                "attempts": 1,
                "usage": {},
            }

    asyncio.run(
        request_summary(
            RunCompactor(),
            Runtime(),
            messages,
            SimpleNamespace(deadline_monotonic=time.monotonic() + 180),
            threading.Event(),
            {},
            {},
            {},
            20000,
        )
    )
    assert len(calls) == 2
    assert calls[1][0] == calls[0][0]
    with pytest.raises(DataError) as error:
        validate_summary(
            {"background": "example", "source_ids": ["q_example_month"], "gaps": []}, {}
        )
    assert error.value.code == "COMPACT_INVALID_SOURCE"


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


def test_format_classification_and_source_priority():
    valid = {"background": {}, "source_ids": [], "gaps": []}
    with pytest.raises(DataError) as error:
        validate_summary(valid, {})
    assert error.value.details["classification"] == "field_type"
    assert error.value.details["field"] == "background"
    with pytest.raises(DataError) as error:
        validate_summary(dict(valid, source_ids=["forged"]), {})
    assert error.value.details["classification"] == "source"


def test_ranges_and_single_coverage_copy():
    from metric_pilot.agent.compaction import encode_coverage

    coverage = {
        "a": [
            {
                "row_indices": [0, 1, 2, 5],
                "state": "partial",
                "returned_rows": 9,
                "artifact_truncated": True,
            }
        ]
    }
    assert encode_coverage(coverage)["a"][0]["row_ranges"] == [[0, 3], [5, 6]]
    summary = validate_summary(
        {"background": "ok", "source_ids": ["a"], "gaps": []},
        {"a": {"visible_coverage": coverage["a"]}},
        coverage=coverage,
    )
    assert "visible_coverage" not in summary["sources"]["a"]
    assert summary["coverage_encoding"] == "row_ranges_v1"


@pytest.mark.parametrize("body", ["{broken", '{"background":{},"source_ids":[],"gaps":[]}'])
def test_summary_parser_keeps_only_bounded_final_body(body):
    from metric_pilot.agent.provider import AgentProvider

    provider = AgentProvider(None)
    raw = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": body, "reasoning_content": "private"}}
        ]
    }
    assert provider.parse(raw, 1, summary=True)["summary_body"] == body
    assert "summary_body" not in provider.parse(raw, 1)
    raw["choices"][0]["message"]["content"] = "x" * 2049
    assert "summary_body" not in provider.parse(raw, 1, summary=True)


@pytest.mark.parametrize(
    "first, expected",
    [
        ({"background": {}, "source_ids": [], "gaps": []}, 2),
        (None, 2),
        ({"background": {}, "source_ids": ["forged"], "gaps": []}, 1),
    ],
)
def test_repair_uses_existing_two_send_budget(first, expected):
    from metric_pilot.agent.compaction import request_summary

    calls = []

    class Runtime:
        async def wait_model(self, messages, *args, **kwargs):
            calls.append(messages)
            return {
                "candidate": first
                if len(calls) == 1
                else {"background": "recovered", "source_ids": [], "gaps": []},
                "summary_body": json.dumps(first),
                "usage": {"total_tokens": 3},
                "attempts": 1,
            }

    compactor = RunCompactor()
    usage = {}

    async def execute():
        return await request_summary(
            compactor,
            Runtime(),
            [{"role": "user", "content": "frozen"}],
            SimpleNamespace(deadline_monotonic=time.monotonic() + 180),
            threading.Event(),
            usage,
            {},
            {},
            10000,
        )

    if expected == 1:
        with pytest.raises(DataError):
            asyncio.run(execute())
    else:
        summary, records = asyncio.run(execute())
        assert summary["background"] == "recovered"
        assert len(records) == 2
        assert calls[1][:1] == calls[0]
    assert len(calls) == expected == usage["model_requests"] == compactor.attempts
    assert usage["total_tokens"] == 3 * expected


@pytest.mark.parametrize("mode", ["exhausted", "time", "capacity", "twice", "oversize"])
def test_repair_failure_never_sends_third_or_changes_prefix(mode):
    from metric_pilot.agent.compaction import request_summary

    compactor = RunCompactor()
    compactor.attempts = int(mode == "exhausted")
    calls = []

    class Runtime:
        async def wait_model(self, messages, *args, **kwargs):
            calls.append(messages)
            if mode == "time":
                ctx.deadline_monotonic = time.monotonic() + 39
            return {
                "candidate": None,
                "summary_body": "x" * (2049 if mode == "oversize" else 20),
                "usage": {},
                "attempts": 1,
            }

    ctx = SimpleNamespace(deadline_monotonic=time.monotonic() + 180)
    with pytest.raises(DataError):
        asyncio.run(
            request_summary(
                compactor,
                Runtime(),
                [{"role": "user", "content": "frozen"}],
                ctx,
                threading.Event(),
                {},
                {},
                {},
                1 if mode == "capacity" else 10000,
            )
        )
    assert len(calls) == (2 if mode == "twice" else 1)
    assert compactor.attempts <= 2


def test_local_summary_timeout_awaits_task_exit():
    stopped = []

    class Provider:
        current_attempts = 1

        async def next_summary(self, messages):
            try:
                await asyncio.sleep(5)
            finally:
                stopped.append(True)

    runtime = SimpleNamespace(provider=Provider(), check=AgentRuntime.check)
    # Bind the ordinary run guard, preserving cancellation priority.
    runtime.check = lambda ctx, cancel: AgentRuntime.check(runtime, ctx, cancel)
    ctx = SimpleNamespace(deadline_monotonic=time.monotonic() + 180)
    with pytest.raises(DataError) as error:
        asyncio.run(
            AgentRuntime.wait_model(
                runtime, [], [], ctx, threading.Event(), summary=True, local_timeout=0.01
            )
        )
    assert error.value.code == "COMPACT_LOCAL_TIMEOUT"
    assert stopped == [True]


def test_summary_utf8_body_limit():
    from metric_pilot.agent.provider import AgentProvider

    provider = AgentProvider(None)
    for size, retained in [(682, True), (683, False)]:
        raw = {"choices": [{"finish_reason": "stop", "message": {"content": chr(0x4E2D) * size}}]}
        assert ("summary_body" in provider.parse(raw, 1, summary=True)) == retained


@pytest.mark.parametrize("indices", [[], [0], list(range(100)), [0, 2, 3, 8]])
def test_range_encoding_round_trip(indices):
    from metric_pilot.agent.compaction import encode_coverage

    pages = {"a": [{"row_indices": indices}, {"row_indices": [20, 21]}]}
    encoded = encode_coverage(pages)["a"]
    assert len(encoded) == 2
    assert [i for start, end in encoded[0]["row_ranges"] for i in range(start, end)] == indices


def test_fixed_metadata_capacity_rejects_before_send():
    from metric_pilot.agent.compaction import request_summary

    class Runtime:
        async def wait_model(self, *args, **kwargs):
            raise AssertionError("fixed metadata cannot fit")

    compactor = RunCompactor()
    with pytest.raises(DataError) as error:
        asyncio.run(
            request_summary(
                compactor,
                Runtime(),
                [],
                SimpleNamespace(deadline_monotonic=time.monotonic() + 180),
                threading.Event(),
                {},
                {},
                {"a": [{"row_indices": [i * 2 for i in range(1000)]}]},
                64000,
            )
        )
    assert error.value.code == "COMPACT_SUMMARY_TOO_LARGE"
    assert compactor.attempts == 0


def test_summary_full_utf8_size_limit_remains():
    with pytest.raises(DataError) as error:
        validate_summary({"background": "\u4e2d" * 900, "source_ids": [], "gaps": []}, {})
    assert error.value.code == "COMPACT_SUMMARY_TOO_LARGE"


def test_summary_parser_utf8_body_boundary():
    from metric_pilot.agent.provider import AgentProvider

    raw = {"choices": [{"finish_reason": "stop", "message": {"content": "\u4e2d" * 682 + "xx"}}]}
    assert len(AgentProvider(None).parse(raw, 1, summary=True)["summary_body"].encode()) == 2048
    raw["choices"][0]["message"]["content"] += "x"
    assert "summary_body" not in AgentProvider(None).parse(raw, 1, summary=True)


def test_failed_summary_response_usage_is_retained(monkeypatch):
    import httpx
    from metric_pilot.agent.provider import AgentProvider
    from test_agent_provider_failures import config, install_transport

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
            },
        )

    install_transport(monkeypatch, handler)
    with pytest.raises(DataError) as error:
        asyncio.run(AgentProvider(config()).next_summary([]))
    assert error.value.code == "MODEL_OUTPUT_INCOMPLETE"
    assert error.value.details["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert len(requests) == error.value.details["attempts"] == 1


@pytest.mark.parametrize(
    "summary",
    [
        {
            "background": "历史自由文字：剩余行未在上下文可见",
            "verification": "unverified_background",
        },
        {
            "background": "本摘要覆盖的前缀仅含前两页",
            "input_coverage": {"a": [{"row_ranges": [[0, 200]]}]},
        },
    ],
)
def test_summary_reuse_preserves_tail_and_adds_prefix_boundary(agent_workspace, summary):
    import copy

    run = enqueue(agent_workspace, investigation(agent_workspace))
    registry = registry_for(agent_workspace, run)
    registry.history_enabled = True
    registry.working_summary = summary
    frozen = copy.deepcopy(summary)
    tail = [[{"role": "assistant", "content": "retained-third-page-200-300"}]]
    messages = fit_context([], tail, registry, 64000)
    background = next(
        json.loads(m["content"]) for m in messages if "working_summary" in (m.get("content") or "")
    )
    assert background["working_summary"] == frozen == summary
    assert "input_coverage不是整个Run的读取清单" in background["boundary"]
    assert messages[-1]["content"] == "retained-third-page-200-300"
