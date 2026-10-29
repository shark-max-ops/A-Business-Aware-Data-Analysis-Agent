"""阶段 0 的公共 API 契约；由 OpenAPI 生成前端类型。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class StrictDTO(BaseModel):
    """
    作用：拒绝契约外字段
    输入：契约字段
    输出：严格校验的 DTO
    """

    model_config = ConfigDict(extra="forbid")


class ErrorDetail(StrictDTO):
    """
    作用：统一错误信息并提供请求追踪标识
    输入：错误码、说明、脱敏细节和请求标识
    输出：错误详情
    """

    code: str
    message: str
    details: dict[str, Any]
    request_id: str


class ErrorResponse(StrictDTO):
    """
    作用：提供统一错误响应外层
    输入：error - 错误详情
    输出：HTTP 错误响应
    """

    error: ErrorDetail


class HealthResponse(StrictDTO):
    """
    作用：描述本地服务可用性与真实功能状态
    输入：元数据库、自检和集成配置状态
    输出：健康检查响应，不包含凭据或本机路径
    """

    status: Literal["ok", "degraded"]
    stage: Literal[1] = 1
    database: Literal["ready", "migration_required", "unavailable"]
    fts5: Literal["available", "unavailable"]
    worker: Literal["not_implemented"] = "not_implemented"
    llm: Literal["configured_unverified", "not_configured"]
    embedding: Literal["configured_unverified", "not_configured"]
    analysis: Literal["not_implemented"] = "not_implemented"
