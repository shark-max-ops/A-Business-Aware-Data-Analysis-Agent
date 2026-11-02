"""模拟传输故障验证有限重试、脱敏、取消及真实 SQL 不重复执行。"""

import asyncio
import json
import threading
import time

import httpx
import pytest
import test_agent
from metric_pilot.agent import models as am
from metric_pilot.agent.contracts import RunDTO
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.agent.runtime import AgentRuntime
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import RunContext
from metric_pilot.settings import Settings
from sqlalchemy import func, select
from test_agent import new_run

agent_workspace = test_agent.agent_workspace
PRIVATE = "https://private.invalid/token=do-not-store"


def install_transport(monkeypatch, handler):
    """
    作用：替换 HTTP 传输边界，不请求网络或读取本机密钥
    输入：隔离补丁与请求处理器
    输出：无
    """
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "metric_pilot.agent.provider.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def config():
    """
    作用：建立不读取真实凭据的模型配置
    输入：无
    输出：隔离配置
    """
    return Settings(
        _env_file=None,
        llm_base_url="https://model.invalid/v1",
        llm_model="test-model",
        llm_api_key="test-key",
    )


def response(candidate):
    """
    作用：生成明确的测试模型最终响应
    输入：最终候选
    输出：HTTP 响应
    """
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(candidate)}}]
        },
    )


@pytest.mark.parametrize(
    "fault", [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError]
)
def test_transient_transport_retries_same_model_request(monkeypatch, fault):
    """
    作用：暂时性异常最多三次且每次消息和工具签名一致
    输入：模拟故障种类
    输出：无
    """
    requests = []

    def handler(request):
        """
        作用：前两次失败，第三次恢复
        输入：真实序列化 HTTP 请求
        输出：模拟响应
        """
        requests.append(request.content)
        if len(requests) < 3:
            raise fault(PRIVATE, request=request)
        return response(
            {"kind": "clarification", "question": "缺少数据", "missing_requirements": ["收入"]}
        )

    install_transport(monkeypatch, handler)
    turn = asyncio.run(
        AgentProvider(config()).next_turn([{"role": "user", "content": "合成问题"}], [])
    )
    assert turn["attempts"] == len(requests) == 3
    assert len(set(requests)) == 1
    assert PRIVATE not in json.dumps(turn)


@pytest.mark.parametrize("status", [400, 401, 403])
def test_auth_and_invalid_requests_are_not_retried(monkeypatch, status):
    """
    作用：认证与参数拒绝立即失败，不重复付费请求
    输入：HTTP 错误码
    输出：无
    """
    seen = []

    def handler(request):
        """
        作用：返回不应重试的 HTTP 状态
        输入：请求
        输出：响应
        """
        seen.append(request)
        return httpx.Response(status, text=PRIVATE)

    install_transport(monkeypatch, handler)
    with pytest.raises(DataError) as failure:
        asyncio.run(AgentProvider(config()).next_turn([], []))
    assert failure.value.code == "MODEL_HTTP_ERROR"
    assert failure.value.details == {"status": status, "attempts": 1}
    assert len(seen) == 1
    assert PRIVATE not in str(failure.value.details)


def test_invalid_response_has_separate_safe_diagnostic(monkeypatch):
    """
    作用：响应不是 JSON 时不冒充网络中断或保存原始内容
    输入：隔离传输
    输出：无
    """
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=PRIVATE))
    with pytest.raises(DataError) as failure:
        asyncio.run(AgentProvider(config()).next_turn([], []))
    assert failure.value.code == "MODEL_RESPONSE_INVALID"
    assert failure.value.details == {"failure_kind": "invalid_json", "attempts": 1}
    assert PRIVATE not in str(failure.value)


def test_partial_response_is_closed_before_retry(monkeypatch):
    """
    作用：读取响应途中断开时关闭失败响应，再重试同一模型请求
    输入：中途失败的异步响应流
    输出：无
    """

    class InterruptedStream(httpx.AsyncByteStream):
        """
        作用：模拟返回部分 JSON 后连接断开
        输入：无
        输出：受控失败响应流
        """

        closed = False

        async def __aiter__(self):
            """
            作用：先返回部分字节，再报告读取中断
            输入：无
            输出：字节片段
            """
            yield b'{"choices":'
            raise httpx.ReadError(PRIVATE)

        async def aclose(self):
            """
            作用：记录响应是否被真实关闭
            输入：无
            输出：无
            """
            self.closed = True

    stream, requests = InterruptedStream(), []

    def handler(request):
        """
        作用：第二次请求确认首个响应已关闭后恢复
        输入：请求
        输出：模拟响应
        """
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=stream)
        assert stream.closed
        return response(
            {"kind": "clarification", "question": "缺数据", "missing_requirements": ["数据"]}
        )

    install_transport(monkeypatch, handler)
    turn = asyncio.run(AgentProvider(config()).next_turn([], []))
    assert turn["attempts"] == len(requests) == 2
    assert stream.closed


@pytest.mark.parametrize("cancelled", [True, False])
def test_runtime_cancels_retry_backoff_and_counts_started_request(monkeypatch, cancelled):
    """
    作用：退避期间仍受显式取消与运行总期限控制
    输入：取消或总期限场景
    输出：无
    """
    requests = []

    def handler(request):
        """
        作用：首个请求触发退避
        输入：请求
        输出：连接异常
        """
        requests.append(request)
        raise httpx.ConnectError(PRIVATE, request=request)

    install_transport(monkeypatch, handler)
    signal = threading.Event()
    timer = threading.Timer(0.08, signal.set) if cancelled else None
    provider = AgentProvider(config())
    ctx = RunContext(
        "run_synthetic",
        "snapshot_synthetic",
        "knowledge_synthetic",
        time.monotonic() + (2 if cancelled else 0.15),
    )
    began = time.monotonic()
    if timer:
        timer.start()
    try:
        with pytest.raises(DataError) as failure:
            asyncio.run(AgentRuntime(None, config(), provider).wait_model([], [], ctx, signal))
    finally:
        if timer:
            timer.join()
    assert failure.value.code == ("RUN_CANCELLED" if cancelled else "RUN_TIMEOUT")
    assert failure.value.details["attempts"] == len(requests) == 1
    assert time.monotonic() - began < 1


@pytest.mark.parametrize("recovered", [True, False])
def test_transport_retries_do_not_repeat_successful_sql(agent_workspace, monkeypatch, recovered):
    """
    作用：真实 SQL 执行后模型传输失败时只重试当前模型请求，保留成功与失败真实状态
    输入：隔离数据库与可恢复或不可恢复故障
    输出：无
    """
    config, store, _, _ = agent_workspace
    run = new_run(agent_workspace)
    requests = []

    def handler(request):
        """
        作用：查询后模拟传输中断，再返回结果或持续失败
        输入：真实模型消息
        输出：模拟响应或安全传输异常
        """
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_actual_sql",
                                        "type": "function",
                                        "function": {
                                            "name": "query_sql",
                                            "arguments": json.dumps(
                                                {"sql": "SELECT COUNT(*) AS n FROM v_flights"}
                                            ),
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        if len(requests) < 4 or not recovered:
            raise httpx.ReadError(PRIVATE, request=request)
        result = next(
            json.loads(m["content"])
            for m in reversed(requests[-1]["messages"])
            if m["role"] == "tool"
        )
        return response({"kind": "query_result", "artifact_ids": result["artifact_ids"]})

    install_transport(monkeypatch, handler)
    asyncio.run(AgentRuntime(store, config, AgentProvider(config)).run(run, threading.Event()))
    actual = RunDTO.model_validate(store.read(run["id"]))
    assert len(requests) == actual.usage["model_requests"] == 4
    assert requests[1] == requests[2] == requests[3]
    with store.engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(am.executions)
                .where(am.executions.c.run_id == run["id"])
            ).scalar_one()
            == 1
        )
    if recovered:
        assert actual.status == "completed" and actual.error_code is None
    else:
        assert actual.status == "failed" and actual.error_code == "MODEL_REQUEST_FAILED"
        assert actual.error_details == {"failure_kind": "connection_interrupted", "attempts": 3}
        last = store.events_after(run["id"], 0)[-1]
        assert last["payload"]["error_details"] == actual.error_details
    assert PRIVATE not in actual.model_dump_json()
