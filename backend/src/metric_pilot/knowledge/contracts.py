"""知识 API 边界与模型输出契约。"""

from datetime import date
from typing import Any, Literal

from pydantic import Field, model_validator

from metric_pilot.contracts import StrictDTO

SourceKind = Literal["official_reference", "project_policy"]


class DocumentMetadata(StrictDTO):
    """
    作用：记录来源权威与适用范围
    输入：来源元数据
    输出：文档来源契约
    """

    source_kind: SourceKind
    origin_url: str | None = Field(default=None, max_length=2000)
    retrieved_at: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    raw_sources: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=4000)
    separate_project_annotations: bool = False

    @model_validator(mode="after")
    def valid_range(self):
        """
        作用：拒绝倒置生效期和不安全来源协议
        输入：当前对象
        输出：当前对象
        """
        if self.effective_from and self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("invalid effective range")
        if self.origin_url and not self.origin_url.startswith(("https://", "http://")):
            raise ValueError("invalid source URL")
        if self.source_kind == "official_reference" and not self.origin_url:
            raise ValueError("official reference requires origin URL")
        return self


class DocumentImport(StrictDTO):
    """
    作用：约束原文导入和乐观锁参数
    输入：原文与来源信息
    输出：导入请求
    """

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=500_000)
    metadata: DocumentMetadata
    document_id: str | None = None
    expected_version: int = Field(default=0, ge=0)


class DocumentRevisionDTO(StrictDTO):
    """
    作用：展示不可变原文与版本
    输入：持久化记录
    输出：原文修订
    """

    id: str
    document_id: str
    version: int
    content: str
    content_hash: str
    metadata: DocumentMetadata
    created_at: str


class ChunkDTO(StrictDTO):
    """
    作用：返回真实摘录及定位
    输入：分块记录
    输出：可核对的片段
    """

    id: str
    revision_id: str
    text: str
    text_hash: str
    source_kind: SourceKind
    locator: dict[str, Any]


class Citation(StrictDTO):
    """
    作用：绑定片段和逐字摘录
    输入：片段标识、摘录及哈希
    输出：引用
    """

    chunk_id: str
    excerpt: str = Field(min_length=1, max_length=12000)
    text_hash: str


class WikiEdit(StrictDTO):
    """
    作用：创建有来源的人工 Wiki 草稿
    输入：正文、引用、输入修订和版本
    输出：草稿请求
    """

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    citations: list[Citation] = Field(min_length=1, max_length=100)
    input_revision_ids: list[str] = Field(min_length=1, max_length=30)
    expected_version: int = Field(ge=0)


class WikiCandidate(StrictDTO):
    """
    作用：限制生成模型只能产出带引用的文本草稿
    输入：结构化模型响应
    输出：Wiki 候选
    """

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=30000)
    citations: list[Citation] = Field(min_length=1, max_length=50)


class VersionAction(StrictDTO):
    """
    作用：约束发布、回滚与审核目标
    输入：目标修订和当前版本
    输出：版本操作
    """

    revision_id: str
    expected_version: int = Field(ge=1)


class GenerateWiki(StrictDTO):
    """
    作用：固定本次生成的来源与编辑基线
    输入：文档修订和页面 slug
    输出：生成请求
    """

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    input_revision_ids: list[str] = Field(min_length=1, max_length=20)
    expected_version: int = Field(default=0, ge=0)


class MetricDefinition(StrictDTO):
    """
    作用：固定计算口径、来源与适用期
    输入：蓝图指标字段
    输出：结构化指标定义
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    authority: Literal["official_mapping", "project_policy"]
    review_state: Literal["draft", "approved"]
    unit: Literal["count", "percent", "minutes"]
    grain: str = Field(min_length=1, max_length=2000)
    numerator_description: str = Field(min_length=1, max_length=4000)
    denominator_description: str | None
    population_filters: list[str] = Field(max_length=50)
    required_columns: list[str] = Field(min_length=1, max_length=100)
    null_policy: str = Field(min_length=1, max_length=4000)
    effective_from: date
    effective_to: date | None = None
    source_revision_ids: list[str] = Field(min_length=1, max_length=30)
    template_key: str | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        """
        作用：校验半开区间
        输入：当前指标
        输出：当前指标
        """
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("invalid effective dates")
        return self


class MetricWrite(StrictDTO):
    """
    作用：绑定指标写入和乐观锁
    输入：定义与预期版本
    输出：写入请求
    """

    definition: MetricDefinition
    expected_version: int = Field(default=0, ge=0)


class JobDTO(StrictDTO):
    """
    作用：显示持久化任务状态，不暴露内部输入
    输入：任务记录
    输出：任务状态
    """

    id: str
    kind: str
    state: Literal["queued", "running", "succeeded", "failed"]
    stage: str
    result: dict[str, Any]
    error_code: str | None
    created_at: str


class ImportReceipt(StrictDTO):
    """
    作用：返回固定原文及实际处理任务
    输入：导入结果
    输出：上传回执
    """

    document_id: str
    revision_id: str
    job_id: str


class SearchRequest(StrictDTO):
    """
    作用：固定检索范围和时间
    输入：问题、知识快照与日期
    输出：检索请求
    """

    query: str = Field(min_length=1, max_length=500)
    snapshot_id: str
    as_of: date | None = None
    end_exclusive: date | None = None
    mode: Literal["text", "hybrid"] = "text"

    @model_validator(mode="after")
    def valid_search_range(self):
        """
        作用：范围检索必须有开始，结束不包含且严格大于开始
        输入：当前请求
        输出：当前请求或验证失败
        """
        if self.end_exclusive and (not self.as_of or self.end_exclusive <= self.as_of):
            raise ValueError("INVALID_SEARCH_DATE_RANGE")
        return self


class SearchHit(StrictDTO):
    """
    作用：展示命中及真实来源
    输入：片段与排序分
    输出：检索结果
    """

    chunk: ChunkDTO
    score: float
    excerpt: str = ""
    match_reason: str = "相关资料片段"


class SearchResult(StrictDTO):
    """
    作用：如实呈现检索模式与限制
    输入：命中及模式
    输出：检索响应
    """

    items: list[SearchHit]
    mode: Literal["text", "hybrid"]
    warnings: list[str]
    wiki_items: list[dict[str, Any]] = Field(default_factory=list)


class ItemList(StrictDTO):
    """
    作用：提供知识目录列表外层
    输入：目录记录
    输出：统一列表
    """

    items: list[dict[str, Any]]
    next_cursor: str | None = None


class GraphNode(StrictDTO):
    """
    作用：返回可导航的真实知识实体
    输入：实体标识、友好标签和来源
    输出：图节点
    """

    id: str
    kind: Literal["document", "concept", "wiki", "metric"]
    label: str
    source_kind: str
    status: str | None = None
    document_id: str | None = None
    revision_id: str | None = None
    chunk_id: str | None = None
    metric_key: str | None = None


class GraphEdge(StrictDTO):
    """
    作用：表达真实引用或来源关系
    输入：起点、终点及关系名
    输出：图连线
    """

    source: str
    target: str
    label: str


class GraphResult(StrictDTO):
    """
    作用：固定图所对应的 Wiki 修订和阅读边界
    输入：节点、连线和范围
    输出：关系图响应
    """

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    page_id: str
    wiki_revision_id: str
    scope: str
    note: str
