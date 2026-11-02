"""可取消的原生工具调用适配；不保存模型内部思维正文。"""

import asyncio
import json

import httpx
from pydantic import ValidationError

from metric_pilot.agent.contracts import ToolCall
from metric_pilot.data.common import DataError


def reject_json_constant(value):
    """
    作用：拒绝 JSON 中非法的非有限数字
    输入：非标准常量名称
    输出：无，抛出参数错误
    """
    raise ValueError("NONFINITE_JSON")


class AgentProvider:
    """
    作用：隔离单一模型 HTTP 协议
    输入：配置
    输出：模型适配器
    """

    def __init__(self, settings):
        """
        作用：绑定配置
        输入：配置
        输出：无
        """
        self.settings = settings
        self.current_attempts = 0

    async def next_turn(self, messages, tools):
        """
        作用：获取白名单响应并有限重试
        输入：消息与工具
        输出：调用或最终候选
        """
        if not self.settings.integration_configured("llm"):
            raise DataError("MODEL_NOT_CONFIGURED")
        base = self.settings.llm_base_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            **({"tools": tools} if tools else {}),
            "max_tokens": self.settings.run_output_tokens,
            "temperature": 0,
            **(
                {"reasoning_effort": self.settings.agent_reasoning_effort}
                if self.settings.agent_reasoning_effort
                else {}
            ),
        }
        headers = {"Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()}
        self.current_attempts = 0
        try:
            async with httpx.AsyncClient(
                timeout=60, trust_env=False, follow_redirects=False
            ) as client:
                for attempt in range(3):
                    self.current_attempts = attempt + 1
                    try:
                        async with client.stream(
                            "POST", url, json=body, headers=headers
                        ) as response:
                            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                                retry_http = True
                            else:
                                retry_http = False
                                if response.status_code != 200:
                                    raise DataError(
                                        "MODEL_HTTP_ERROR", {"status": response.status_code}
                                    )
                                parts, size = [], 0
                                async for part in response.aiter_bytes():
                                    size += len(part)
                                    if size > 2 * 1024 * 1024:
                                        raise DataError("MODEL_RESPONSE_TOO_LARGE")
                                    parts.append(part)
                        if retry_http:
                            # 退出响应上下文后退避，不保持失败连接或重跑工具。
                            await asyncio.sleep(0.5 * 2**attempt)
                            continue
                        try:
                            raw = json.loads(b"".join(parts), parse_constant=reject_json_constant)
                        except (ValueError, UnicodeError):
                            raise DataError(
                                "MODEL_RESPONSE_INVALID", {"failure_kind": "invalid_json"}
                            ) from None
                        return self.parse(raw, attempt + 1)
                    except (
                        httpx.TimeoutException,
                        httpx.NetworkError,
                        httpx.RemoteProtocolError,
                    ) as error:
                        if attempt < 2:
                            await asyncio.sleep(0.5 * 2**attempt)
                            continue
                        kind = (
                            "timeout"
                            if isinstance(error, httpx.TimeoutException)
                            else "connection"
                            if isinstance(error, httpx.ConnectError)
                            else "connection_interrupted"
                        )
                        # 只保留分类和次数，异常正文可能带 URL 或其他敏感信息。
                        raise DataError("MODEL_REQUEST_FAILED", {"failure_kind": kind}) from None
        except DataError as exc:
            exc.details["attempts"] = self.current_attempts
            raise
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise DataError(
                "MODEL_REQUEST_FAILED",
                {"attempts": self.current_attempts, "failure_kind": "request_error"},
            ) from None

    def parse(self, raw, attempts):
        """
        作用：丢弃 reasoning_content 等扩展正文
        输入：原始响应
        输出：标准化回合
        """
        try:
            choice = raw["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise DataError("MODEL_OUTPUT_INCOMPLETE")
            message = choice["message"]
            usage = {
                k: v
                for k, v in raw.get("usage", {}).items()
                if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and type(v) is int
                and v >= 0
            }
            calls = []
            for item in message.get("tool_calls") or []:
                if item.get("type") != "function":
                    raise ValueError()
                # 参数 JSON 无效作为可修复工具错误反馈，不把原始模型文本落库。
                try:
                    arguments = json.loads(
                        item["function"]["arguments"], parse_constant=reject_json_constant
                    )
                    if not isinstance(arguments, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    arguments = {"__invalid_json__": True}
                # 丢弃模型可能夹带的思维元字段，不进入上下文、事件或工具审计。
                arguments = {
                    key: value
                    for key, value in arguments.items()
                    if key
                    not in {
                        "thought",
                        "thoughts",
                        "reasoning",
                        "reasoning_content",
                        "chain_of_thought",
                    }
                }
                calls.append(
                    ToolCall(id=item["id"], name=item["function"]["name"], arguments=arguments)
                )
            if calls:
                if len({c.id for c in calls}) != len(calls):
                    raise ValueError()
                assistant = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": json.dumps(c.arguments, ensure_ascii=False),
                            },
                        }
                        for c in calls
                    ],
                }
                return {
                    "calls": calls,
                    "assistant": assistant,
                    "usage": usage,
                    "attempts": attempts,
                }
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError()
            try:
                candidate = json.loads(content, parse_constant=reject_json_constant)
            except ValueError:
                candidate = None
            return {"calls": [], "candidate": candidate, "usage": usage, "attempts": attempts}
        except DataError:
            raise
        except (KeyError, TypeError, ValueError, IndexError, ValidationError):
            raise DataError("MODEL_PROTOCOL_ERROR") from None
