"""运行与工具契约；兼容历史查询结果并区分已提交报告。"""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from metric_pilot.contracts import StrictDTO
from metric_pilot.data.contracts import ExplorationScope
from metric_pilot.external.weather import WeatherRequest
from metric_pilot.reports.contracts import ReportCandidate, ReportResult, TextBlock, ValueRef

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
    investigation_id: str | None = None
    expected_investigation_version: int | None = Field(default=None, ge=1)


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
    error_details: dict[str, str | int] = Field(default_factory=dict)
    cancel_requested: bool
    created_at: str
    seq: int
    model: str
    usage: dict[str, int]
    investigation_id: str | None = None
    expected_investigation_version: int | None = None


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
    external_snapshot_id: str | None = None


class SQLArgs(StrictDTO):
    """
    作用：提出受限 SQL
    输入：SQL、标量绑定及规则
    输出：工具参数
    """

    sql: str = Field(min_length=1, max_length=20000)
    params: dict[str, Any] = Field(default_factory=dict)
    metric_revision_ids: list[str] = Field(default_factory=list, max_length=16)
    purpose: Literal["standard", "exploratory", "reviewed_metric"] = "standard"
    exploration_scope: ExplorationScope | None = None
    external_snapshot_id: str | None = None

    @model_validator(mode="after")
    def check_purpose(self):
        """
        作用：拒绝无范围探索、探索冒绑指标和未绑定版本的正式指标计算
        输入：已通过字段校验的参数
        输出：用途一致的参数
        """
        if self.purpose == "exploratory":
            if self.exploration_scope is None or self.metric_revision_ids:
                raise ValueError("探索需说明范围，且不能绑定正式指标")
        elif self.exploration_scope is not None:
            raise ValueError("探索说明仅用于探索查询")
        if self.purpose == "reviewed_metric" and not self.metric_revision_ids:
            raise ValueError("正式指标计算必须绑定已审核修订")
        return self


class ArtifactArgs(StrictDTO):
    """
    作用：读取本次成功产物
    输入：ID 与有限分页
    输出：工具参数
    """

    artifact_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class ExternalSourceArgs(StrictDTO):
    """
    作用：读取本轮候选或授权固定外部来源，拒绝任意 URL。
    输入：服务端标识与有限原文分页。
    输出：严格工具参数。
    """

    source_ref: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=50)


class WeatherArgs(WeatherRequest):
    """
    作用：向模型明确开放字段枚举，避免猜测天气字段名称。
    输入：已限定的机场、日期和字段。
    输出：与独立天气接口一致的严格参数。
    """

    fields: list[
        Literal["temperature", "wind_speed", "visibility", "precipitation", "snow_depth"]
    ] = Field(
        default_factory=lambda: ["temperature", "wind_speed", "visibility"],
        min_length=1,
        max_length=5,
    )


class CreateInvestigation(StrictDTO):
    """
    作用：请求固定调查边界或显式分支
    输入：会话、目标、范围及快照
    输出：严格请求
    """

    conversation_id: str
    goal: str = Field(min_length=1, max_length=1200)
    scope: str = Field(min_length=1, max_length=1200)
    snapshot_id: str
    parent_id: str | None = None


class RecordFinding(StrictDTO):
    """
    作用：记录核查后的发现与待解决问题
    输入：简短核查、分类、引用模板及后续问题
    输出：待验证发现，不含模型内部思维
    """

    kind: Literal["fact", "candidate", "counterevidence", "limitation", "recommendation"]
    check: str = Field(min_length=1, max_length=400)
    statement: TextBlock
    questions: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=6
    )
    supersedes: str | None = None


class ReadInvestigation(StrictDTO):
    """
    作用：有限分页读取调查发现，防止历史记录挤满模型上下文
    输入：偏移和上限
    输出：严格分页参数
    """

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=10)


class FindingDTO(StrictDTO):
    """
    作用：展示服务端已取值的固定发现
    输入：持久化发现
    输出：中文内容与证据索引
    """

    id: str
    run_id: str
    kind: str
    check: str
    text: str
    questions: list[str]
    supersedes: str | None
    created_at: str
    artifact_ids: list[str]
    evidence_ids: list[str]
    value_refs: dict[str, ValueRef]
    source_run_status: RunStatus


class InvestigationDTO(StrictDTO):
    """
    作用：恢复调查、额度与发现历史
    输入：服务端调查
    输出：前后端统一契约
    """

    id: str
    conversation_id: str
    goal: str
    scope: str
    snapshot_id: str
    knowledge_snapshot_id: str
    parent_id: str | None
    created_at: str
    version: int
    limits: dict[str, int]
    allocated: dict[str, int]
    status: Literal["ready", "running", "budget_exhausted"]
    active_run_id: str | None
    run_ids: list[str]
    findings: list[FindingDTO]
