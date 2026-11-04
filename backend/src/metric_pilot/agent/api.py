"""运行 API 与按 SQLite 游标轮询的无遗漏 SSE。"""

import asyncio
import json

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from metric_pilot.agent import models as m
from metric_pilot.agent.contracts import (
    ConversationDTO,
    CreateInvestigation,
    CreateRun,
    EventDTO,
    InvestigationDTO,
    RunDTO,
)
from metric_pilot.agent.investigations import InvestigationStore
from metric_pilot.agent.store import TERMINAL, RunStore
from metric_pilot.data.api import get_catalog
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import Page
from metric_pilot.knowledge import models as km
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.store import KnowledgeStore

router = APIRouter(prefix="/api/v1")


def store_for(request):
    """
    作用：检查可用服务
    输入：请求
    输出：运行仓储
    """
    get_catalog(request)
    return RunStore(request.app.state.engine)


@router.post("/conversations", response_model=ConversationDTO, status_code=201)
def create_conversation(request: Request):
    """
    作用：建立分析会话
    输入：请求
    输出：会话
    """
    return store_for(request).conversation()


@router.get("/conversations", response_model=Page[ConversationDTO])
def conversations(request: Request):
    """
    作用：恢复持久会话列表
    输入：请求
    输出：会话列表
    """
    store = store_for(request)
    with store.engine.connect() as c:
        return {
            "items": [
                dict(r)
                for r in c.execute(
                    select(m.conversations).order_by(m.conversations.c.created_at.desc())
                ).mappings()
            ]
        }


@router.get("/conversations/{conversation_id}", response_model=Page[RunDTO])
def conversation(conversation_id: str, request: Request):
    """
    作用：恢复同会话所有运行
    输入：会话 ID
    输出：运行列表
    """
    store = store_for(request)
    with store.engine.connect() as c:
        if not c.execute(
            select(m.conversations).where(m.conversations.c.id == conversation_id)
        ).first():
            raise DataError("CONVERSATION_NOT_FOUND")
        return {
            "items": [
                store.public(dict(r))
                for r in c.execute(
                    select(m.runs)
                    .where(m.runs.c.conversation_id == conversation_id)
                    .order_by(m.runs.c.id)
                ).mappings()
            ]
        }


@router.post("/runs", response_model=RunDTO, status_code=202)
def create_run(body: CreateRun, request: Request):
    """
    作用：冻结依赖后排队
    输入：问题、数据快照及可选上文
    输出：运行
    """
    store = store_for(request)
    settings = request.app.state.settings
    worker = request.app.state.agent_worker
    if not worker or not worker.thread.is_alive():
        raise DataError("ANALYSIS_WORKER_NOT_READY")
    if not settings.integration_configured("llm"):
        raise DataError("MODEL_NOT_CONFIGURED")
    snapshot = get_catalog(request).snapshot(body.snapshot_id)
    knowledge = KnowledgeStore(store.engine)
    fixed = (
        knowledge.snapshot()
        if not body.investigation_id
        else {
            "id": InvestigationStore(store.engine).read(body.investigation_id)[
                "knowledge_snapshot_id"
            ]
        }
    )
    with store.engine.connect() as c:
        fixed = {"id": fixed["id"], **knowledge.get(c, km.snapshots, fixed["id"])["payload"]}
    # 用户选中的指标必须属于服务端刚冻结的已审核清单，且覆盖整月。
    if not set(body.metric_revision_ids) <= set(fixed["metric_revision_ids"]):
        raise DataError("METRIC_NOT_IN_SNAPSHOT")
    import calendar
    from datetime import date

    year, month = map(int, snapshot.period.split("-"))
    with store.engine.connect() as c:
        for ident in body.metric_revision_ids:
            metric = knowledge.get(c, km.metric_revisions, ident)
            for day in (1, calendar.monthrange(year, month)[1]):
                found = MetricService(knowledge).resolve(
                    fixed["id"], metric["metric_key"], date(year, month, day).isoformat()
                )
                if found["id"] != ident:
                    raise DataError("METRIC_NOT_APPLICABLE")
    return store.create(
        body,
        fixed["id"],
        {
            "max_turns": settings.run_max_turns,
            "max_tool_calls": settings.run_max_tool_calls,
            "timeout_seconds": settings.run_timeout_seconds,
            "context_tokens": settings.run_context_tokens,
            "output_tokens": settings.run_output_tokens,
            "sql_timeout_seconds": settings.sql_timeout_seconds,
            "external_max_requests": settings.external_max_requests,
            "external_max_seconds": settings.external_max_seconds,
            "external_max_bytes": settings.external_max_bytes,
            "sql_max_rows": settings.sql_max_rows,
        },
        model=settings.llm_model,
    )


@router.post("/investigations", response_model=InvestigationDTO, status_code=201)
def create_investigation(body: CreateInvestigation, request: Request):
    """
    作用：固定新调查或显式分支的范围、知识与累计额度
    输入：调查请求
    输出：调查记录
    """
    store = store_for(request)
    get_catalog(request).snapshot(body.snapshot_id)
    config = request.app.state.settings
    fixed = KnowledgeStore(store.engine).snapshot()
    return InvestigationStore(store.engine).create(
        body,
        fixed["id"],
        {
            "max_turns": config.run_max_turns,
            "max_tool_calls": config.run_max_tool_calls,
            "timeout_seconds": config.run_timeout_seconds,
        },
    )


@router.get("/investigations", response_model=Page[InvestigationDTO])
def list_investigations(request: Request, conversation_id: str):
    """
    作用：恢复同会话的调查列表及分支
    输入：会话标识
    输出：调查列表
    """
    store = InvestigationStore(store_for(request).engine)
    with store.engine.connect() as c:
        ids = list(
            c.execute(
                select(m.investigations.c.id).where(
                    m.investigations.c.conversation_id == conversation_id
                )
            ).scalars()
        )
    return {"items": sorted([store.read(ident) for ident in ids], key=lambda p: p["created_at"])}


@router.get("/investigations/{investigation_id}", response_model=InvestigationDTO)
def get_investigation(investigation_id: str, request: Request):
    """
    作用：恢复调查和发现历史
    输入：调查标识
    输出：调查记录
    """
    return InvestigationStore(store_for(request).engine).read(investigation_id)


@router.get("/investigations/{investigation_id}/findings/{finding_id}/evidence")
def finding_evidence(investigation_id: str, finding_id: str, request: Request):
    """
    作用：读取发现固定证据而非最新值
    输入：调查与发现标识
    输出：证据快照
    """
    return InvestigationStore(store_for(request).engine).evidence(investigation_id, finding_id)


@router.get("/runs/{run_id}", response_model=RunDTO)
def get_run(run_id: str, request: Request):
    """
    作用：读取可刷新状态
    输入：运行 ID
    输出：运行
    """
    return store_for(request).read(run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunDTO)
def cancel_run(run_id: str, request: Request):
    """
    作用：持久化幂等取消再通知执行器
    输入：运行 ID
    输出：当前状态
    """
    row = store_for(request).cancel(run_id)
    worker = request.app.state.agent_worker
    if worker:
        worker.cancel(run_id)
    return row


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
):
    """
    作用：按严格序号重放实际事件
    输入：游标
    输出：SSE
    """
    store = store_for(request)
    store.read(run_id)
    try:
        cursor = max(after, int(last_event_id or 0))
        if cursor < 0:
            raise ValueError()
    except ValueError:
        raise DataError("INVALID_EVENT_CURSOR") from None

    async def stream():
        """
        作用：从数据库持续追读，无注册监听丢失窗口
        输入：无
        输出：帧
        """
        seq, last_heartbeat = cursor, 0.0
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(store.events_after, run_id, seq)
            for event in batch:
                seq = event["seq"]
                body = json.dumps(event, ensure_ascii=False)
                yield f"id: {seq}\nevent: {event['type']}\ndata: {body}\n\n"
            row = await asyncio.to_thread(store.read, run_id)
            if row["status"] in TERMINAL and seq >= row["seq"]:
                break
            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= 10:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(0.2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/event-log", response_model=Page[EventDTO])
def event_log(run_id: str, request: Request, after: int = Query(0, ge=0)):
    """
    作用：提供事件契约及有限历史页
    输入：运行和游标
    输出：事件列表
    """
    items = store_for(request).events_after(run_id, after)
    return {"items": items, "next_cursor": str(items[-1]["seq"]) if len(items) == 100 else None}
