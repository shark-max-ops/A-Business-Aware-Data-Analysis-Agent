"""串行工具循环和有监督后台 worker；显式取消独立于 SSE 连接。"""

import asyncio
import json
import logging
import threading
import time

from pydantic import TypeAdapter, ValidationError

from metric_pilot.agent.context import fit_context, initial_messages
from metric_pilot.agent.contracts import FinalCandidate
from metric_pilot.agent.external_tools import NAMES as EXTERNAL_NAMES
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.middleware import RuntimeObserver
from metric_pilot.agent.provider import AgentProvider
from metric_pilot.agent.titles import split_title
from metric_pilot.agent.tools import ToolRegistry
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError, digest
from metric_pilot.data.contracts import RunContext, ToolResult
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.data.ranges import run_range
from metric_pilot.reports.service import ReportService


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
            date_range=run_range(self.store.engine, run),
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
        closing = False
        last_phase = "investigate"
        try:
            previous = [
                {
                    "question": self.store.read(ident)["question"],
                    "result": self.store.read(ident)["result"],
                }
                for ident in run["context_run_ids"]
            ]
            header = initial_messages(run, registry, previous)
            for index in range(budget["max_turns"]):
                self.check(ctx, cancel)
                allowed = set(registry.specs())
                if run.get("investigation_id"):
                    remaining = budget["max_tool_calls"] - used
                    turns_left = budget["max_turns"] - index
                    reserve = min(5, max(1, budget["max_turns"] // 3))
                    closing = (
                        closing
                        or turns_left <= reserve
                        or remaining <= 4
                        or (
                            ctx.deadline_monotonic - time.monotonic() <= 30
                            and budget["timeout_seconds"] > 30
                        )
                    )
                    phase = "investigate"
                    needs_checkpoint = False
                    if closing:
                        phase = (
                            "finalize"
                            if turns_left <= min(3, reserve)
                            or remaining == 0
                            or (remaining <= 2 and registry.recorded_count)
                            else "prepare"
                        )
                        needs_checkpoint = (
                            phase == "finalize"
                            and not registry.recorded_count
                            and remaining > 0
                            and turns_left > 1
                        )
                        allowed &= (
                            {"read_artifact", "record_finding"}
                            if phase == "prepare"
                            else {"record_finding"}
                            if needs_checkpoint
                            else set()
                        )
                    registry.progress = {
                        "remaining_turns": turns_left,
                        "remaining_tool_calls": remaining,
                        "phase": phase,
                        "successful_queries": len(registry.query_summaries),
                        "recorded_findings": registry.recorded_count,
                        "instruction": (
                            "收尾前尚缺本轮记录；当前仅允许record_finding保存已核实发现或真实限制。"
                            "statement必须含type=text、id、text_template、values、evidence_ids；"
                            "不能新增查询或把未来计划冒充已完成事实，保存后立即提交报告。"
                            if needs_checkpoint
                            else "现在只提交最终报告或如实说明缺少的关键条件，不再调用工具。"
                            "报告只用已有成功产物与 ValueRef；未完成核查写入限制，不编造结论。"
                            if phase == "finalize"
                            else "收尾准备：不得新增查询或检索。读取已有证据，"
                            "尽快 record_finding 保存本轮发现或限制，再提交简洁报告。"
                            if phase == "prepare"
                            else "已有成功核查但尚无记录时，"
                            "先 record_finding 保存观察或限制，再继续核查。"
                            if registry.query_summaries and not registry.recorded_count
                            else "按已有结果选择有价值的核查；不要重复已完成的结构与范围查询。"
                        ),
                    }
                    if phase != last_phase:
                        self.store.emit(
                            run["id"],
                            "run.progress",
                            {
                                "phase": phase,
                                "summary": "正在整理已有发现与报告"
                                if phase == "prepare"
                                else "正在生成最终结果，不再新增查询",
                            },
                        )
                        last_phase = phase
                schemas = [s for s in registry.schemas() if s["function"]["name"] in allowed]
                # 按本轮实际开放的工具签名预留空间，总上下文预算保持不变。
                tool_bytes = len(
                    json.dumps(schemas, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                available = budget["context_tokens"] - budget["output_tokens"] - tool_bytes - 1024
                turn_header = (
                    header + [{"role": "system", "content": registry.progress["instruction"]}]
                    if run.get("investigation_id") and closing
                    else header
                )
                messages = fit_context(turn_header, groups, registry, available)
                self.observer.before_model(run["id"], index + 1)
                turn = await self.wait_model(messages, schemas, ctx, cancel)
                for key, value in turn["usage"].items():
                    usage[key] = usage.get(key, 0) + value
                usage["model_requests"] = usage.get("model_requests", 0) + turn["attempts"]
                calls = turn["calls"]
                if not calls:
                    candidate = None
                    try:
                        payload, conversation_title = split_title(turn.get("candidate"))
                        candidate = TypeAdapter(FinalCandidate).validate_python(payload)
                        if candidate.kind == "query_result":
                            for ident in candidate.artifact_ids:
                                query.read_artifact(run["id"], ident, 0, 1)
                        prepared = None
                        if candidate.kind == "report":
                            if run.get("investigation_id"):
                                findings = InvestigationStore(self.store.engine).read(
                                    run["investigation_id"]
                                )["findings"]
                                if not any(entry["run_id"] == run["id"] for entry in findings):
                                    raise DataError("INVESTIGATION_RECORD_REQUIRED")
                            self.store.emit(
                                run["id"], "run.progress", {"summary": "正在核对报告数字与依据"}
                            )
                            prepared = await asyncio.to_thread(
                                ReportService(query).prepare, run, candidate.report
                            )
                        self.check(ctx, cancel)
                        self.observer.on_finish(
                            run["id"],
                            "completed",
                            None if prepared is not None else candidate.model_dump(),
                            usage=usage,
                            conversation_title=conversation_title,
                            prepared_report=prepared,
                            deadline=ctx.deadline_monotonic,
                            cancel=cancel,
                        )
                        return
                    except (ValidationError, DataError) as exc:
                        if isinstance(exc, DataError) and exc.code in {
                            "RUN_CANCELLED",
                            "RUN_TIMEOUT",
                        }:
                            raise
                        code = (
                            exc.code if isinstance(exc, DataError) else "REPORT_STRUCTURE_INVALID"
                        )
                        details = (
                            exc.details
                            if isinstance(exc, DataError)
                            else {
                                "fields": [
                                    {
                                        "location": list(e["loc"][1:])
                                        if e["loc"]
                                        and e["loc"][0]
                                        in {"report", "query_result", "clarification"}
                                        else list(e["loc"]),
                                        "type": e["type"],
                                        "constraints": {
                                            key: value
                                            for key, value in e.get("ctx", {}).items()
                                            if key in {"max_length", "min_length", "actual_length"}
                                            and isinstance(value, (int, str))
                                        },
                                    }
                                    for e in exc.errors()[:12]
                                ]
                            }
                        )
                        repairs += 1
                        self.store.emit(
                            run["id"],
                            "run.progress",
                            dict(
                                phase="report_repair",
                                summary="正在修正报告结构或引用",
                                error_code=code,
                                fields=details.get("fields", []),
                            ),
                        )
                        if repairs > 2:
                            raise DataError("FINAL_CANDIDATE_INVALID") from None
                        groups.append(
                            [
                                {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        candidate.model_dump(exclude_defaults=True),
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    if candidate is not None
                                    else "最终候选结构无效，请按契约重建。",
                                },
                                {
                                    "role": "user",
                                    "content": json.dumps(
                                        {
                                            "error_code": code,
                                            "details": details,
                                            "instruction": (
                                                "按报告契约修正；仅引用本次或服务端允许的同调查成功产物。"
                                                "只输出JSON对象，不加Markdown围栏；外层kind=report与report，"
                                                "report直接包含schema_version、title、scope、blocks等草稿字段。"
                                                "保留已核对的内容和引用含义，只修正所报错误；"
                                                "原文文字直接写模板，不得替换为无关查询数字。"
                                                "INVESTIGATION_RECORD_REQUIRED 时"
                                                "先核查并 record_finding。"
                                                "截断需重新聚合查询。"
                                            ),
                                        },
                                        ensure_ascii=False,
                                    ),
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
                pending_clarification = None
                for call in calls:
                    execution = self.observer.before_tool(run["id"], call)
                    began = time.monotonic()
                    try:
                        self.check(ctx, cancel)
                        if stop_code:
                            raise DataError(stop_code)
                        if pending_clarification:
                            result = ToolResult(
                                ok=False,
                                summary="需先补充已审核口径，本批后续工具未执行",
                                error_code="METRIC_REVIEW_OR_CLARIFICATION_REQUIRED",
                            )
                        elif call.name in registry.specs() and call.name not in allowed:
                            result = ToolResult(
                                ok=False,
                                summary="已进入收尾，该工具未执行；请使用已有证据完成结果",
                                error_code="TOOL_NOT_AVAILABLE_DURING_WRAP_UP",
                            )
                        else:
                            result = await registry.invoke_async(call)
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
                    if call.name in EXTERNAL_NAMES:
                        result.data["source_requests"] = registry.external.last_fetches
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
                    if not result.ok and not pending_clarification:
                        repeated = repeated + 1 if signature == previous_failure else 1
                        previous_failure = signature
                        if repeated >= 2:
                            if (
                                result.error_code == "METRIC_REVIEW_OR_CLARIFICATION_REQUIRED"
                                and registry.blocked_metrics
                            ):
                                # 先给模型一次选择独立探索的机会；重复原查询仍以澄清结束。
                                pending_clarification = {
                                    "kind": "clarification",
                                    "question": "相关项目指标缺少已审核、日期适用的口径，"
                                    "重复查询已停止。正式指标需先人工审核，"
                                    "再显式建立使用新知识版本的调查或分支；"
                                    "原始数据探索可另行说明统计范围与缺失处理后进行。",
                                    "missing_requirements": [
                                        f"需要人工审核且日期适用的项目指标：{key}"
                                        for key in sorted(registry.blocked_metrics)
                                    ],
                                }
                            elif call.name not in EXTERNAL_NAMES:
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
                if pending_clarification:
                    self.check(ctx, cancel)
                    self.observer.on_finish(
                        run["id"], "completed", pending_clarification, usage=usage
                    )
                    return
            raise DataError("TURN_BUDGET_EXCEEDED")
        except DataError as exc:
            if exc.details.get("attempts"):
                usage["model_requests"] = usage.get("model_requests", 0) + exc.details["attempts"]
            self.observer.on_finish(
                run["id"],
                "cancelled" if exc.code == "RUN_CANCELLED" else "failed",
                error_code=exc.code,
                error_details=exc.details,
                usage=usage,
            )
        except Exception:
            logging.getLogger(__name__).exception("Agent runtime failed: %s", run["id"])
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
        if hasattr(self.provider, "current_attempts"):
            self.provider.current_attempts = 0
        task = asyncio.create_task(self.provider.next_turn(messages, tools))
        try:
            while not task.done():
                self.check(ctx, cancel)
                await asyncio.wait({task}, timeout=0.05)
            self.check(ctx, cancel)
            return await task
        except DataError as error:
            if error.code in {"RUN_TIMEOUT", "RUN_CANCELLED"}:
                error.details["attempts"] = getattr(self.provider, "current_attempts", 0)
            raise
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
