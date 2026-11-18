import asyncio
from types import SimpleNamespace

import pytest
from metric_pilot.agent.compaction import summary_diagnostic


def test_diagnostic_whitelists_transport_and_drops_body():
    events = []
    runtime = SimpleNamespace(store=SimpleNamespace(emit=lambda *args: events.append(args)))
    summary_diagnostic(
        runtime,
        SimpleNamespace(run_id="r"),
        error_code="MODEL_HTTP_ERROR",
        status=429,
        failure_kind="timeout",
        attempts=1,
        body="SECRET",
        usage={"prompt_tokens": 3, "reasoning": "SECRET"},
    )
    result = events[0][2]["compaction"]
    assert result["status"] == 429
    assert result["attempts"] == 1
    assert "SECRET" not in str(result)


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_projection_single_send(monkeypatch, status):
    import httpx
    from metric_pilot.agent.diagnostics import safe_diagnostics
    from metric_pilot.agent.provider import AgentProvider
    from metric_pilot.data.common import DataError
    from test_agent_provider_failures import config, install_transport

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="SECRET")

    install_transport(monkeypatch, handler)
    with pytest.raises(DataError) as error:
        asyncio.run(AgentProvider(config()).next_turn([], [], max_attempts=1))
    projected = safe_diagnostics(error.value.details)
    assert projected["status"] == status
    assert projected["attempts"] == len(requests) == 1
    assert "SECRET" not in str(projected)


def test_timeout_single_send_and_unknown_status(monkeypatch):
    import httpx
    from metric_pilot.agent.diagnostics import safe_diagnostics
    from metric_pilot.agent.provider import AgentProvider
    from metric_pilot.data.common import DataError
    from test_agent_provider_failures import config, install_transport

    sends = []

    def handler(request):
        sends.append(1)
        raise httpx.ReadTimeout("SECRET URL")

    install_transport(monkeypatch, handler)
    with pytest.raises(DataError) as error:
        asyncio.run(AgentProvider(config()).next_turn([], [], max_attempts=1))
    projected = safe_diagnostics(error.value.details)
    assert projected["failure_kind"] == "timeout"
    assert projected["attempts"] == len(sends) == 1
    assert "status" not in projected
    assert "SECRET" not in str(projected)


def test_recorder_preserves_memory_body_and_counts_failed_followup(monkeypatch, tmp_path):
    import copy

    from controlled_trace import Trace, totals
    from metric_pilot.agent.provider import AgentProvider
    from metric_pilot.data.common import DataError
    from test_agent_provider_failures import config

    turn = {
        "summary_body": "SECRET",
        "attempts": 1,
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    original = copy.deepcopy(turn)

    async def respond(self, *args, **kwargs):
        self.current_attempts = 1
        if len(self.records) == 1:
            return turn
        raise DataError("MODEL_HTTP_ERROR", {"status": 500, "attempts": 1})

    monkeypatch.setattr(AgentProvider, "next_turn", respond)
    provider = Trace(config(), tmp_path)

    async def run():
        assert await provider.next_turn([], [], summary=True) is turn
        with pytest.raises(DataError):
            await provider.next_turn([], [])

    asyncio.run(run())
    assert turn == original
    assert "SECRET" not in (tmp_path / "requests.json").read_text()
    assert totals(provider.records) == {
        "model_requests": 2,
        "usage_complete": False,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }
