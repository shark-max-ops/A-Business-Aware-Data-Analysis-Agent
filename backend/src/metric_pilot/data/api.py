"""数据源与成功产物读取 API；不向页面或模型接受数据库路径。"""

from fastapi import APIRouter, Depends, Query, Request

from metric_pilot.data.catalog import Catalog
from metric_pilot.data.common import DataError
from metric_pilot.data.contracts import (
    ActivateRequest,
    ArtifactPage,
    Page,
    SchemaDTO,
    SnapshotDTO,
    SourceDTO,
)
from metric_pilot.data.executor import QueryExecutor
from metric_pilot.data.query import QueryService
from metric_pilot.database import inspect_database

router = APIRouter(prefix="/api/v1")


def get_catalog(request: Request) -> Catalog:
    """
    作用：为 API 绑定服务端工作区并检查迁移状态
    输入：当前请求
    输出：目录服务
    """
    if inspect_database(request.app.state.engine)[0] != "ready":
        raise DataError("MIGRATION_REQUIRED")
    return Catalog(request.app.state.engine, request.app.state.settings.storage_dir)


@router.get("/sources", response_model=Page[SourceDTO])
def list_sources(catalog: Catalog = Depends(get_catalog)):
    """
    作用：读取真实来源列表，空数据库返回空列表
    输入：工作区目录
    输出：来源列表
    """
    return Page[SourceDTO](items=catalog.sources())


@router.get("/sources/{source_id}/snapshots", response_model=Page[SnapshotDTO])
def list_snapshots(source_id: str, catalog: Catalog = Depends(get_catalog)):
    """
    作用：读取固定来源的历史快照
    输入：来源标识与目录
    输出：快照列表
    """
    return Page[SnapshotDTO](items=catalog.snapshots(source_id))


@router.post("/sources/{source_id}/activate", response_model=SourceDTO)
def activate(source_id: str, body: ActivateRequest, catalog: Catalog = Depends(get_catalog)):
    """
    作用：显式设置默认快照，不覆盖旧文件
    输入：来源标识、严格请求体与目录
    输出：来源状态
    """
    return catalog.activate(source_id, body.snapshot_id)


@router.get("/sources/{source_id}/schema", response_model=SchemaDTO)
def schema(source_id: str, snapshot_id: str | None = None, catalog: Catalog = Depends(get_catalog)):
    """
    作用：读取允许查询的固定业务结构
    输入：来源、可选快照与目录
    输出：表结构
    """
    return catalog.schema(source_id, snapshot_id)


@router.get("/runs/{run_id}/artifacts/{artifact_id}", response_model=ArtifactPage)
def read_artifact(
    run_id: str,
    artifact_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    catalog: Catalog = Depends(get_catalog),
):
    """
    作用：验证运行关联后读取成功产物，不执行查询
    输入：运行和产物标识、分页范围与目录
    输出：有限结果及其元数据
    """
    return QueryService(catalog, QueryExecutor()).read_artifact(run_id, artifact_id, offset, limit)
