"""运行与工具契约；兼容历史查询结果并区分已提交报告。"""

from typing import Annotated, Any, Literal

from pydantic import Field

from metric_pilot.contracts import StrictDTO
from metric_pilot.reports.contracts import ReportCandidate, ReportResult

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]


class ConversationDTO(StrictDTO):
    """
    作用：描述会话
    输入：服务端记录
    输出：公开会话
    """

    id: str
    title: str
    created_at: str


class CreateRun(StrictDTO):
    """
    作用：请求分析
    输入：问题与已登记快照
    输出：严格请求
    """

    conversation_id: str
    question: str = Field(min_length=1, max_length=4000)
    snapshot_id: str
    metric_revision_ids: list[str] = Field(default_factory=list, max_length=16)
    context_run_ids: list[str] = Field(default_factory=list, max_length=4)


class QueryResult(StrictDTO):
    """
    作用：提交查询产物清单
    输入：当前运行产物
    输出：阶段 3 结果
    """

    kind: Literal["query_result"]
    artifact_ids: list[str] = Field(min_length=1, max_length=24)


class Clarification(StrictDTO):
    """
    作用：结束需补充条件的运行
    输入：问题与缺失项
    输出：澄清结果
    """

    kind: Literal["clarification"]
    question: str = Field(min_length=1, max_length=1200)
    missing_requirements: list[str] = Field(min_length=1, max_length=10)


FinalCandidate = Annotated[
    ReportCandidate | QueryResult | Clarification, Field(discriminator="kind")
]
StoredResult = Annotated[ReportResult | QueryResult | Clarification, Field(discriminator="kind")]


class RunDTO(StrictDTO):
    """
    作用：恢复运行状态
    输入：持久化记录
    输出：前后端一致 DTO
    """

    id: str
    conversation_id: str
    question: str
    snapshot_id: str
    knowledge_snapshot_id: str
    status: RunStatus
    budget: dict[str, int]
    context_run_ids: list[str]
    metric_revision_ids: list[str]
    result: StoredResult | None = None
    error_code: str | None = None
    cancel_requested: bool
    created_at: str
    seq: int
    model: str
    usage: dict[str, int]


class EventDTO(StrictDTO):
    """
    作用：统一 SSE envelope
    输入：事务分配事件
    输出：可重放事件
    """

    run_id: str
    seq: int
    type: Literal[
        "run.queued",
        "run.started",
        "run.progress",
        "tool.started",
        "tool.finished",
        "report.ready",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    ]
    created_at: str
    payload: dict[str, Any]


class ToolCall(StrictDTO):
    """
    作用：隔离模型工具协议
    输入：适配器白名单字段
    输出：工具调用
    """

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]


class SearchArgs(StrictDTO):
    """
    作用：限制资料检索
    输入：关键词及上限
    输出：工具参数
    """

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=8)


class WikiArgs(StrictDTO):
    """
    作用：读取快照内 Wiki
    输入：页面 ID
    输出：工具参数
    """

    page_id: str


class MetricArgs(StrictDTO):
    """
    作用：解析固定月份的指标
    输入：指标键
    输出：工具参数
    """

    metric_key: str


class EmptyArgs(StrictDTO):
    """
    作用：拒绝权限参数
    输入：空对象
    输出：空参数
    """


class TableArgs(StrictDTO):
    """
    作用：查看允许表
    输入：表名
    输出：工具参数
    """

    table_name: str


class SQLArgs(StrictDTO):
    """
    作用：提出受限 SQL
    输入：SQL、标量绑定及规则
    输出：工具参数
    """

    sql: str = Field(min_length=1, max_length=20000)
    params: dict[str, Any] = Field(default_factory=dict)
    metric_revision_ids: list[str] = Field(default_factory=list, max_length=16)


class ArtifactArgs(StrictDTO):
    """
    作用：读取本次成功产物
    输入：ID 与有限分页
    输出：工具参数
    """

    artifact_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
