"""串行工具循环和有监督后台 worker；显式取消独立于 SSE 连接。"""

import asyncio
import json
import threading
import time

from pydantic import TypeAdapter, ValidationError

from metric_pilot.agent.context import fit_context, initial_messages
from metric_pilot.agent.contracts import FinalCandidate
from metric_pilot.agent.middleware import RuntimeObserver
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.agent.titles import split_title
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, digest
from metric_pilot.data.contracts import RunContext, ToolResult
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService


class AgentRuntime:
    """
    作用：控制模型与工具预算
    输入：仓储、配置和模型
    输出：可执行运行框架
    """

    def __init__(self, store, settings, provider):
        """
        作用：组合依赖
        输入：仓储、配置和模型
        输出：无
        """
        self.store, self.settings, self.provider = store, settings, provider
        self.observer = RuntimeObserver(store)

    async def run(self, run, cancel):
        """
        作用：执行一次固定运行
        输入：已领取任务与取消信号
        输出：持久终态
        """
        budget = run["budget"]
        ctx = RunContext(
            run["id"],
            run["snapshot_id"],
            run["knowledge_snapshot_id"],
            time.monotonic() + budget["timeout_seconds"],
        )
        query = QueryService(
            Catalog(self.store.engine, self.settings.storage_dir),
            QueryExecutor(
                budget.get("sql_timeout_seconds", self.settings.sql_timeout_seconds),
                budget.get("sql_max_rows", self.settings.sql_max_rows),
            ),
        )
        registry = ToolRegistry(run, ctx, query, self.settings, cancel)
        groups, used, repairs, timeout_repairs = [], 0, 0, 0
        seen, previous_failure, repeated = set(), None, 0
        usage = {}
        try:
            previous = [
                {
                    "question": self.store.read(ident)["question"],
                    "result": self.store.read(ident)["result"],
                }
                for ident in run["context_run_ids"]
            ]
            header = initial_messages(run, registry, previous)
            tool_bytes = len(json.dumps(registry.schemas(), ensure_ascii=False).encode("utf-8"))
            available = budget["context_tokens"] - budget["output_tokens"] - tool_bytes - 1024
            for index in range(budget["max_turns"]):
                self.check(ctx, cancel)
                messages = fit_context(header, groups, registry, available)
                self.observer.before_model(run["id"], index + 1)
                turn = await self.wait_model(messages, registry.schemas(), ctx, cancel)
                for key, value in turn["usage"].items():
                    usage[key] = usage.get(key, 0) + value
                usage["model_requests"] = usage.get("model_requests", 0) + turn["attempts"]
                calls = turn["calls"]
                if not calls:
                    try:
                        payload, conversation_title = split_title(turn.get("candidate"))
                        candidate = TypeAdapter(FinalCandidate).validate_python(payload)
                        if candidate.kind == "query_result":
                            for ident in candidate.artifact_ids:
                                query.read_artifact(run["id"], ident, 0, 1)
                        self.check(ctx, cancel)
                        self.observer.on_finish(
                            run["id"],
                            "completed",
                            candidate.model_dump(),
                            usage=usage,
                            conversation_title=conversation_title,
                        )
                        return
                    except (ValidationError, DataError):
                        repairs += 1
                        if repairs > 2:
                            raise DataError("FINAL_CANDIDATE_INVALID") from None
                        groups.append(
                            [
                                {"role": "assistant", "content": "最终候选待修正"},
                                {
                                    "role": "user",
                                    "content": "结果结构或产物引用无效，请按 JSON 契约修正。",
                                },
                            ]
                        )
                        continue
                if used + len(calls) > budget["max_tool_calls"]:
                    raise DataError("TOOL_BUDGET_EXCEEDED")
                if any(call.id in seen for call in calls):
                    raise DataError("DUPLICATE_TOOL_CALL_ID")
                seen.update(call.id for call in calls)
                # 完整原生 assistant 工具批次单独留存，不保存模型内部正文。
                self.store.emit(
                    run["id"],
                    "run.progress",
                    {"summary": "开始工具调用", "assistant": turn["assistant"]},
                )
                group = [turn["assistant"]]
                stop_code = None
                for call in calls:
                    execution = self.observer.before_tool(run["id"], call)
                    began = time.monotonic()
                    try:
                        self.check(ctx, cancel)
                        if stop_code:
                            raise DataError(stop_code)
                        result = await asyncio.to_thread(registry.invoke, call)
                    except DataError as exc:
                        result = ToolResult(
                            ok=False, summary="工具未执行：" + exc.code, error_code=exc.code
                        )
                        stop_code = exc.code
                    except Exception:
                        result = ToolResult(
                            ok=False, summary="工具内部错误", error_code="TOOL_INTERNAL_ERROR"
                        )
                        stop_code = "TOOL_INTERNAL_ERROR"
                    if cancel.is_set():
                        result = ToolResult(ok=False, summary="已取消", error_code="RUN_CANCELLED")
                        stop_code = "RUN_CANCELLED"
                    self.observer.after_tool(
                        run["id"],
                        execution,
                        call.name,
                        result,
                        int((time.monotonic() - began) * 1000),
                    )
                    used += 1
                    group.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result.model_dump(), ensure_ascii=False),
                        }
                    )
                    signature = digest({"name": call.name, "arguments": call.arguments})
                    if not result.ok:
                        repeated = repeated + 1 if signature == previous_failure else 1
                        previous_failure = signature
                        if repeated >= 2:
                            stop_code = "REPEATED_TOOL_FAILURE"
                        if result.error_code == "QUERY_TIMEOUT":
                            timeout_repairs += 1
                            if timeout_repairs > 2:
                                stop_code = "QUERY_TIMEOUT_REPAIR_EXCEEDED"
                    else:
                        previous_failure, repeated = None, 0
                groups.append(group)
                if stop_code:
                    raise DataError(stop_code)
            raise DataError("TURN_BUDGET_EXCEEDED")
        except DataError as exc:
            if exc.details.get("attempts"):
                usage["model_requests"] = usage.get("model_requests", 0) + exc.details["attempts"]
            self.observer.on_finish(
                run["id"],
                "cancelled" if exc.code == "RUN_CANCELLED" else "failed",
                error_code=exc.code,
                usage=usage,
            )
        except Exception:
            self.observer.on_finish(
                run["id"], "failed", error_code="RUN_INTERNAL_ERROR", usage=usage
            )

    def check(self, ctx, cancel):
        """
        作用：强制取消及总时间限制
        输入：上下文与信号
        输出：无
        """
        if cancel.is_set():
            raise DataError("RUN_CANCELLED")
        if time.monotonic() >= ctx.deadline_monotonic:
            raise DataError("RUN_TIMEOUT")

    async def wait_model(self, messages, tools, ctx, cancel):
        """
        作用：取消时关闭实际异步 HTTP
        输入：模型请求及运行限制
        输出：模型回合
        """
        task = asyncio.create_task(self.provider.next_turn(messages, tools))
        try:
            while not task.done():
                self.check(ctx, cancel)
                await asyncio.wait({task}, timeout=0.05)
            self.check(ctx, cancel)
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class AgentWorker:
    """
    作用：监督单并发分析任务
    输入：仓储和配置
    输出：后台 worker
    """

    def __init__(self, store, settings):
        """
        作用：建立本地监督器
        输入：依赖
        输出：无
        """
        self.store, self.settings = store, settings
        self.stop = threading.Event()
        self.current_cancel = None
        self.current_id = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.loop, name="analysis-worker", daemon=True)

    def start(self):
        """
        作用：恢复遗留状态后启动
        输入：无
        输出：无
        """
        self.store.recover()
        self.thread.start()

    def cancel(self, ident):
        """
        作用：通知当前实际执行
        输入：运行标识
        输出：无
        """
        with self.lock:
            if ident == self.current_id and self.current_cancel:
                self.current_cancel.set()

    def loop(self):
        """
        作用：串行领取并执行任务
        输入：无
        输出：无
        """
        while not self.stop.is_set():
            run = self.store.claim()
            if run is None:
                self.stop.wait(0.1)
                continue
            with self.lock:
                self.current_id, self.current_cancel = run["id"], threading.Event()
                if self.stop.is_set() or self.store.read(run["id"])["cancel_requested"]:
                    self.current_cancel.set()
            try:
                frozen = self.settings.model_copy(
                    update={
                        "run_output_tokens": run["budget"]["output_tokens"],
                        "llm_model": run.get("model") or self.settings.llm_model,
                    }
                )
                asyncio.run(
                    AgentRuntime(self.store, frozen, AgentProvider(frozen)).run(
                        run, self.current_cancel
                    )
                )
            except Exception:
                self.store.finish(run["id"], "failed", error_code="RUN_INTERNAL_ERROR")
            finally:
                with self.lock:
                    self.current_id, self.current_cancel = None, None

    def close(self):
        """
        作用：停止领取并回收当前工作
        输入：无
        输出：无
        """
        self.stop.set()
        with self.lock:
            if self.current_cancel:
                self.current_cancel.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("AGENT_WORKER_NOT_STOPPED")
