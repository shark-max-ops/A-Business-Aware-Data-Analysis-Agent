"""FastAPI 工厂、统一错误处理和不产生付费调用的健康检查。"""

import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from metric_pilot.agent.api import router as agent_router
from metric_pilot.agent.runtime import AgentWorker
from metric_pilot.agent.store import RunStore
from metric_pilot.contracts import ErrorDetail, ErrorResponse, HealthResponse
from metric_pilot.data.api import router as data_router
from metric_pilot.data.common import DataError
from metric_pilot.data.executor import CONNECTION_CONFIG
from metric_pilot.database import create_metadata_engine, inspect_database
from metric_pilot.external import models as external_models  # noqa: F401
from metric_pilot.knowledge.api import router as knowledge_router
from metric_pilot.knowledge.jobs import KnowledgeWorker
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.reports.api import router as reports_router
from metric_pilot.settings import Settings

logger = logging.getLogger(__name__)


def error_response(request: Request, status: int, code: str, message: str, details=None):
    """
    作用：构建统一脱敏错误响应
    输入：请求、HTTP 状态、错误码、消息和可选安全细节
    输出：JSONResponse
    """
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details or {},
            request_id=request.state.request_id,
        )
    )
    return JSONResponse(status_code=status, content=body.model_dump())


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    作用：组合配置、数据库、知识与分析监督器和 API
    输入：settings - 可选配置覆盖值
    输出：FastAPI 应用
    """
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """
        作用：管理元数据库引擎的应用生命周期
        输入：application - FastAPI 应用
        输出：异步生命周期上下文
        """
        import duckdb

        # 启动即验证锁定版本支持全部安全配置，不静默忽略未知设置。
        with duckdb.connect(":memory:", config=CONNECTION_CONFIG):
            pass
        application.state.settings = config
        application.state.engine = create_metadata_engine(config)
        application.state.knowledge_ready = inspect_database(application.state.engine) == (
            "ready",
            "available",
        )
        application.state.knowledge_worker = None
        application.state.agent_worker = None
        if application.state.knowledge_ready:
            worker = KnowledgeWorker(
                KnowledgeStore(application.state.engine), KnowledgeProvider(config)
            )
            worker.start()
            application.state.knowledge_worker = worker
            agent_worker = AgentWorker(RunStore(application.state.engine), config)
            agent_worker.start()
            application.state.agent_worker = agent_worker
        try:
            yield
        finally:
            if application.state.agent_worker:
                application.state.agent_worker.close()
            if application.state.knowledge_worker:
                application.state.knowledge_worker.close()
            application.state.engine.dispose()

    application = FastAPI(
        title="A Business-Aware Data Analysis Agent",
        version="0.1.0",
        lifespan=lifespan,
        responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    )

    @application.middleware("http")
    async def identify_request(request: Request, call_next):
        """
        作用：服务端生成请求标识并转换未预期错误
        输入：request - 请求；call_next - 下游处理器
        输出：带请求标识的响应
        """
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            # 仅记录关联标识，避免把路径或凭据写入响应和日志。
            logger.error("Unhandled request error: %s", request.state.request_id)
            response = error_response(request, 500, "INTERNAL_ERROR", "服务内部错误")
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        """
        作用：将 HTTP 错误转换为统一契约
        输入：request - 请求；exc - HTTP 异常
        输出：错误响应
        """
        response = error_response(
            request,
            exc.status_code,
            f"HTTP_{exc.status_code}",
            "资源不存在" if exc.status_code == 404 else "请求无法处理",
        )
        response.headers.update(exc.headers or {})
        return response

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        """
        作用：返回字段错误位置，排除可能包含秘密的原始输入
        输入：request - 请求；exc - 请求校验异常
        输出：422 错误响应
        """
        fields = [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        return error_response(
            request, 422, "INVALID_ARGUMENT", "请求参数不符合契约", {"fields": fields}
        )

    @application.get(
        "/api/v1/health",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    def health(request: Request):
        """
        作用：报告数据库、FTS5 和配置状态，不执行模型请求
        输入：request - 请求
        输出：健康 DTO，数据库异常时返回 503
        """
        database, fts5 = inspect_database(request.app.state.engine)
        worker_ready = bool(
            request.app.state.knowledge_worker
            and request.app.state.knowledge_worker.thread.is_alive()
        )
        analysis_ready = bool(
            request.app.state.agent_worker and request.app.state.agent_worker.thread.is_alive()
        )
        ready = database == "ready" and fts5 == "available" and worker_ready and analysis_ready
        body = HealthResponse(
            status="ok" if ready else "degraded",
            analysis="ready" if analysis_ready else "not_ready",
            database=database,
            fts5=fts5,
            worker="ready" if worker_ready else "not_ready",
            llm="configured_unverified"
            if config.integration_configured("llm")
            else "not_configured",
            embedding=(
                "configured_unverified"
                if config.integration_configured("embedding")
                else "not_configured"
            ),
        )
        return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())

    @application.exception_handler(DataError)
    async def handle_data_error(request: Request, exc: DataError):
        """
        作用：将数据层安全诊断转换为统一错误契约
        输入：请求与安全数据错误
        输出：HTTP 错误响应
        """
        missing = exc.code.endswith("NOT_FOUND")
        unavailable = exc.code in {
            "MIGRATION_REQUIRED",
            "SNAPSHOT_FILE_UNAVAILABLE",
            "ARTIFACT_FILE_UNAVAILABLE",
        }
        status = 404 if missing else 503 if unavailable else 409
        investigation_messages = {
            "INVESTIGATION_BUSY": "当前调查仍有运行，请等待完成或显式停止",
            "INVESTIGATION_VERSION_CONFLICT": "调查进展已更新，请刷新后继续",
            "INVESTIGATION_BOUNDARY_MISMATCH": "数据或知识版本与调查不一致，请显式新建调查或分支",
            "INVESTIGATION_BUDGET_EXCEEDED": "调查累计额度不足，历史发现与报告仍可查看",
        }
        return error_response(
            request,
            status,
            exc.code,
            investigation_messages.get(
                exc.code,
                "资源不存在"
                if missing
                else "数据服务尚未就绪"
                if unavailable
                else "数据操作无法完成",
            ),
            exc.details,
        )

    application.include_router(reports_router)
    application.include_router(agent_router)
    application.include_router(data_router)
    application.include_router(knowledge_router)
    return application


app = create_app()
