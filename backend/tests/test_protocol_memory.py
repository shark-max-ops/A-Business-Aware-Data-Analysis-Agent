import copy
import json

import pytest
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.data.common import DataError


def test_protocol_roundtrip_and_clear():
    from metric_pilot.agent.protocol import bind, hydrate

    provider = AgentProvider(None)
    raw = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "reasoning_content": "private-canary",
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "read_artifact", "arguments": "{}"},
                        }
                    ],
                },
            }
        ]
    }
    turn = provider.parse(raw, 1)
    message = copy.deepcopy(turn["assistant"])
    assert "private-canary" not in json.dumps(turn, default=lambda x: x.model_dump())
    assert hydrate([message])[0]["reasoning_content"] == "private-canary"
    rewritten = bind({"role": "assistant", "content": "safe repair"}, message)
    assert hydrate([rewritten])[0]["reasoning_content"] == "private-canary"
    provider.end_protocol()
    with pytest.raises(DataError):
        hydrate([message])


def test_protocol_capacity_and_missing():
    from metric_pilot.agent.context import model_message_bytes
    from metric_pilot.agent.protocol import hydrate

    provider = AgentProvider(None)

    def parse(text):
        return provider.parse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "{}", "reasoning_content": text},
                    }
                ]
            },
            1,
        )["assistant"]

    a = parse("x")
    b = parse("x" * 1000)
    assert model_message_bytes([b]) - model_message_bytes([a]) >= 999
    with pytest.raises(DataError):
        hydrate([{"role": "assistant", "tool_calls": [{"id": "forged"}]}])
    provider.end_protocol()


def test_http_hydrates_only_wire_and_cleans(monkeypatch):
    import asyncio

    import httpx
    from test_agent_provider_failures import config, install_transport

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "{}", "reasoning_content": "wire-canary"},
                    }
                ]
            },
        )

    install_transport(monkeypatch, handler)
    provider = AgentProvider(config())

    async def execute():
        first = await provider.next_turn([], [], max_attempts=1)
        await provider.next_turn([copy.deepcopy(first["assistant"])], [], max_attempts=1)
        assert "wire-canary" not in json.dumps(first)
        assert sent[1]["messages"][0]["reasoning_content"] == "wire-canary"
        provider.end_protocol()
        with pytest.raises(DataError):
            await provider.next_turn([first["assistant"]], [], max_attempts=1)
        assert provider.current_attempts == 0

    asyncio.run(execute())
    assert len(sent) == 2


def test_split_and_prefix_release():
    from metric_pilot.agent.context import completed_pairs
    from metric_pilot.agent.protocol import hydrate

    provider = AgentProvider(None)
    raw = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "reasoning_content": "split-canary",
                    "tool_calls": [
                        {
                            "id": x,
                            "type": "function",
                            "function": {"name": "read_artifact", "arguments": "{}"},
                        }
                        for x in ["a", "b"]
                    ],
                },
            }
        ]
    }
    message = provider.parse(raw, 1)["assistant"]
    groups = completed_pairs(
        [
            message,
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "content": "{}"},
        ]
    )
    assert len(groups) == 2
    provider.retain_protocol(groups[-1:])
    assert hydrate(groups[-1])[0]["reasoning_content"] == "split-canary"
    provider.retain_protocol([])
    with pytest.raises(DataError):
        hydrate(groups[0])
