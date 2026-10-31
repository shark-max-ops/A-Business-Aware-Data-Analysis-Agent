"""知识管理 HTTP 边界；不向模型开放写权限。"""

from datetime import date

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import select

from metric_pilot.data.common import DataError, utc_now
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.contracts import (
    ChunkDTO,
    DocumentImport,
    DocumentMetadata,
    DocumentRevisionDTO,
    GenerateWiki,
    GraphResult,
    ImportReceipt,
    ItemList,
    JobDTO,
    MetricWrite,
    SearchRequest,
    SearchResult,
    VersionAction,
    WikiEdit,
)
from metric_pilot.knowledge.documents import DocumentService
from metric_pilot.knowledge.graph import KnowledgeGraph
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.knowledge.wiki import WikiService

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


@router.get("/knowledge/graph", response_model=GraphResult)
def knowledge_graph(page_id: str, request: Request):
    """
    作用：返回选中页面的真实引用关系，不自动发布或重生成
    输入：页面标识和请求
    输出：关系图
    """
    return KnowledgeGraph(store_for(request)).build(page_id)


def job_public(row):
    """
    作用：从任务记录剔除内部请求和幂等键
    输入：任务记录
    输出：公开任务 DTO
    """
    return {key: row[key] for key in JobDTO.model_fields}


def store_for(request):
    """
    作用：从应用组合仓储，不接受客户端文件路径
    输入：请求
    输出：仓储
    """
    if not request.app.state.knowledge_ready:
        raise DataError("MIGRATION_REQUIRED")
    return KnowledgeStore(request.app.state.engine)


def wiki_for(request):
    """
    作用：组合 Wiki 服务
    输入：请求
    输出：Wiki 服务
    """
    return WikiService(store_for(request), KnowledgeProvider(request.app.state.settings))


@router.post("/documents", response_model=ImportReceipt, status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    metadata: str = Form(...),
    document_id: str | None = Form(None),
    expected_version: int = Form(0),
):
    """
    作用：接收有大小限制的 UTF-8 Markdown/TXT
    输入：文件、元数据及可选旧版本
    输出：实际原文和任务标识
    """
    if not file.filename or not file.filename.lower().endswith((".md", ".txt")):
        raise DataError("DOCUMENT_TYPE_UNSUPPORTED")
    raw = await file.read(500001)
    await file.close()
    if len(raw) > 500000:
        raise DataError("DOCUMENT_TOO_LARGE")
    try:
        content = raw.decode("utf-8-sig")
        data = DocumentImport(
            title=title,
            content=content,
            metadata=DocumentMetadata.model_validate_json(metadata),
            document_id=document_id,
            expected_version=expected_version,
        )
    except (UnicodeError, ValidationError, ValueError):
        raise DataError("INVALID_DOCUMENT") from None
    return DocumentService(store_for(request)).import_document(data)


@router.get("/documents", response_model=ItemList)
def list_documents(request: Request):
    """
    作用：展示实际文档与最新处理状态
    输入：请求
    输出：目录
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        items = []
        for row in connection.execute(select(m.documents).order_by(m.documents.c.title)).mappings():
            revision = store.get(connection, m.revisions, row["current_revision_id"])
            job = (
                connection.execute(select(m.jobs).where(m.jobs.c.idempotency_key == revision["id"]))
                .mappings()
                .first()
            )
            items.append(
                {
                    **dict(row),
                    "metadata": revision["metadata"],
                    "content_hash": revision["content_hash"],
                    "job": job_public(job) if job else None,
                }
            )
    return {"items": items}


@router.get("/documents/{document_id}/revisions", response_model=ItemList)
def document_history(document_id: str, request: Request):
    """
    作用：列出文档不可变历史
    输入：文档标识和请求
    输出：历史目录
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        store.get(connection, m.documents, document_id)
        items = [
            dict(r)
            for r in connection.execute(
                select(m.revisions)
                .where(m.revisions.c.document_id == document_id)
                .order_by(m.revisions.c.version.desc())
            ).mappings()
        ]
    return {"items": items}


@router.get("/documents/{document_id}/revisions/{revision_id}", response_model=DocumentRevisionDTO)
def document_revision(document_id: str, revision_id: str, request: Request):
    """
    作用：读取固定原文并验证文档归属
    输入：文档和修订标识
    输出：原文
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        row = store.get(connection, m.revisions, revision_id)
        if row["document_id"] != document_id:
            raise DataError("REVISION_SCOPE_MISMATCH")
        return row


@router.get("/chunks/{chunk_id}", response_model=ChunkDTO)
def chunk_detail(chunk_id: str, request: Request):
    """
    作用：读取真实摘录及原文坐标
    输入：片段标识
    输出：片段
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        return store.get(connection, m.chunks, chunk_id)


@router.get("/documents/{document_id}/revisions/{revision_id}/chunks", response_model=ItemList)
def revision_chunks(document_id: str, revision_id: str, request: Request):
    """
    作用：查看固定原文分块并提供人工引用选择
    输入：文档与修订标识
    输出：可定位片段列表
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        revision = store.get(connection, m.revisions, revision_id)
        if revision["document_id"] != document_id:
            raise DataError("REVISION_SCOPE_MISMATCH")
        return {
            "items": [
                dict(row)
                for row in connection.execute(
                    select(m.chunks).where(m.chunks.c.revision_id == revision_id)
                ).mappings()
            ]
        }


@router.post("/knowledge/snapshots")
def create_snapshot(request: Request) -> dict:
    """
    作用：由服务端冻结可见修订集合
    输入：请求
    输出：知识快照
    """
    return store_for(request).snapshot()


@router.get("/knowledge/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, request: Request) -> dict:
    """
    作用：查看历史知识快照
    输入：快照标识
    输出：固定修订集合
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        return store.get(connection, m.snapshots, snapshot_id)


@router.post("/knowledge/search", response_model=SearchResult)
def search(data: SearchRequest, request: Request):
    """
    作用：在固定范围内执行指定检索模式
    输入：搜索请求
    输出：真实片段与来源
    """
    return Retriever(store_for(request), KnowledgeProvider(request.app.state.settings)).search(data)


@router.post("/knowledge/reindex", response_model=JobDTO, status_code=202)
def reindex(request: Request):
    """
    作用：显式为当前文档排队构建当前 embedding 模型索引
    输入：请求
    输出：实际任务
    """
    if not request.app.state.settings.integration_configured("embedding"):
        raise DataError("MODEL_NOT_CONFIGURED", {"integration": "embedding"})
    store = store_for(request)
    with store.write() as connection:
        ids = list(connection.execute(select(m.documents.c.current_revision_id)).scalars())
        if not ids:
            raise DataError("DOCUMENT_NOT_INDEXED")
        ident = store.enqueue(connection, "embedding", {"revision_ids": ids})
        return job_public(store.get(connection, m.jobs, ident))


@router.get("/jobs", response_model=ItemList)
def list_jobs(request: Request):
    """
    作用：列出近期真实任务
    输入：请求
    输出：任务目录
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        return {
            "items": [
                job_public(row)
                for row in connection.execute(
                    select(m.jobs).order_by(m.jobs.c.created_at.desc()).limit(100)
                ).mappings()
            ]
        }


@router.get("/jobs/{job_id}", response_model=JobDTO)
def get_job(job_id: str, request: Request):
    """
    作用：读取持久化任务状态
    输入：任务标识
    输出：安全状态
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        return job_public(store.get(connection, m.jobs, job_id))


@router.post("/jobs/{job_id}/retry", response_model=JobDTO, status_code=202)
def retry_job(job_id: str, request: Request):
    """
    作用：仅将失败任务重新排队，保留真实错误结果
    输入：失败任务标识
    输出：排队状态
    """
    store = store_for(request)
    with store.write() as connection:
        row = store.get(connection, m.jobs, job_id)
        if row["state"] != "failed":
            raise DataError("JOB_NOT_FAILED")
        history = row["result"].get("attempt_history", [])
        if not history:
            history = [
                {"state": "failed", "error_code": row["error_code"], "retried_at": utc_now()}
            ]
        connection.execute(
            m.jobs.update()
            .where(m.jobs.c.id == job_id)
            .values(
                state="queued",
                stage="queued",
                error_code=None,
                result={**row["result"], "attempt_history": history},
            )
        )
        return job_public(store.get(connection, m.jobs, job_id))


@router.post("/wiki/generate", response_model=JobDTO, status_code=202)
def generate_wiki(data: GenerateWiki, request: Request):
    """
    作用：显式排队生成草稿，不自动发布
    输入：固定文档与页面基线
    输出：生成任务
    """
    service = wiki_for(request)
    ident = service.queue_generation(data)
    with service.store.engine.connect() as connection:
        return job_public(service.store.get(connection, m.jobs, ident))


@router.get("/wiki/pages", response_model=ItemList)
def list_wiki(request: Request):
    """
    作用：返回目录、草稿和发布指针
    输入：请求
    输出：Wiki 页面目录
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        return {
            "items": [
                {
                    **dict(row),
                    "revision": store.get(connection, m.wiki_revisions, row["current_revision_id"]),
                }
                for row in connection.execute(select(m.pages).order_by(m.pages.c.slug)).mappings()
            ]
        }


@router.get("/wiki/pages/{page_id}/revisions", response_model=ItemList)
def wiki_history(page_id: str, request: Request):
    """
    作用：查看 Wiki 历史而不删除旧内容
    输入：页面标识
    输出：历史列表
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        store.get(connection, m.pages, page_id)
        return {
            "items": [
                dict(row)
                for row in connection.execute(
                    select(m.wiki_revisions)
                    .where(m.wiki_revisions.c.page_id == page_id)
                    .order_by(m.wiki_revisions.c.version.desc())
                ).mappings()
            ]
        }


@router.get("/wiki/pages/{page_id}/revisions/{revision_id}")
def wiki_revision(page_id: str, revision_id: str, request: Request) -> dict:
    """
    作用：读取归属于页面的固定修订
    输入：页面和修订标识
    输出：固定正文
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        row = store.get(connection, m.wiki_revisions, revision_id)
        if row["page_id"] != page_id:
            raise DataError("REVISION_SCOPE_MISMATCH")
        return row


@router.put("/wiki/pages/{page_id}")
def edit_wiki(page_id: str, data: WikiEdit, request: Request) -> dict:
    """
    作用：追加人工编辑草稿
    输入：页面和有引用的正文
    输出：新草稿
    """
    service = wiki_for(request)
    with service.store.engine.connect() as connection:
        page = service.store.get(connection, m.pages, page_id)
    payload = data.model_dump(mode="json", exclude={"expected_version"})
    return service.save(page["slug"], payload, data.expected_version)


@router.post("/wiki/pages", status_code=201)
def create_wiki(slug: str, data: WikiEdit, request: Request) -> dict:
    """
    作用：无模型时也能人工建立有引用的页面草稿
    输入：slug、正文与引用
    输出：草稿
    """
    if data.expected_version != 0:
        raise DataError("VERSION_CONFLICT")
    return wiki_for(request).save(
        slug, data.model_dump(mode="json", exclude={"expected_version"}), 0
    )


@router.post("/wiki/pages/{page_id}/publish")
def publish_wiki(page_id: str, data: VersionAction, request: Request) -> dict:
    """
    作用：显式发布当前草稿为新修订
    输入：版本操作
    输出：发布修订
    """
    return wiki_for(request).action(page_id, data, True)


@router.post("/wiki/pages/{page_id}/revert")
def revert_wiki(page_id: str, data: VersionAction, request: Request) -> dict:
    """
    作用：基于历史新增草稿，保留全部历史
    输入：目标历史修订
    输出：新草稿
    """
    return wiki_for(request).action(page_id, data, False)


@router.get("/metrics", response_model=ItemList)
def list_metrics(request: Request):
    """
    作用：列出最新定义与已审核指针
    输入：请求
    输出：指标目录
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        return {
            "items": [
                {
                    **dict(row),
                    "revision": store.get(
                        connection, m.metric_revisions, row["current_revision_id"]
                    ),
                }
                for row in connection.execute(
                    select(m.metrics).order_by(m.metrics.c.key)
                ).mappings()
            ]
        }


@router.post("/metrics", status_code=201)
def create_metric(data: MetricWrite, request: Request) -> dict:
    """
    作用：创建指标草稿，不能通过参数越过审核
    输入：结构化定义
    输出：草稿修订
    """
    if data.expected_version != 0:
        raise DataError("METRIC_CREATE_VERSION_REQUIRED")
    return MetricService(store_for(request)).save(data)


@router.post("/metrics/{key}/revisions", status_code=201)
def edit_metric(key: str, data: MetricWrite, request: Request) -> dict:
    """
    作用：追加指标草稿并验证键归属
    输入：指标键与定义
    输出：新草稿
    """
    if key != data.definition.key:
        raise DataError("REVISION_SCOPE_MISMATCH")
    return MetricService(store_for(request)).save(data)


@router.get("/metrics/{key}/revisions", response_model=ItemList)
def metric_history(key: str, request: Request):
    """
    作用：读取固定指标历史
    输入：指标键
    输出：历史定义
    """
    store = store_for(request)
    with store.engine.connect() as connection:
        store.get(connection, m.metrics, key, "key")
        return {
            "items": [
                dict(row)
                for row in connection.execute(
                    select(m.metric_revisions)
                    .where(m.metric_revisions.c.metric_key == key)
                    .order_by(m.metric_revisions.c.version.desc())
                ).mappings()
            ]
        }


@router.post("/metrics/{key}/approve")
def approve_metric(key: str, data: VersionAction, request: Request) -> dict:
    """
    作用：通过独立操作审核当前指标草稿
    输入：版本操作
    输出：已审核修订
    """
    return MetricService(store_for(request)).approve(key, data)


@router.get("/metrics/{key}/resolve")
def resolve_metric(key: str, snapshot_id: str, as_of: date, request: Request) -> dict:
    """
    作用：按知识快照和日期解析正式指标
    输入：指标键、快照与日期
    输出：适用定义或明确错误
    """
    return MetricService(store_for(request)).resolve(snapshot_id, key, as_of.isoformat())
