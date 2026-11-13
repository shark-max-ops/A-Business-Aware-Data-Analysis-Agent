"""报告契约：模型仅提出引用与布局，服务端物化真实值。"""

from typing import Annotated, Any, Literal

from pydantic import Field

from metric_pilot.contracts import StrictDTO
from metric_pilot.data.contracts import QueryArtifact, SnapshotDTO
from metric_pilot.data.ranges import DateRange

ValueFormat = Literal["integer", "decimal_2", "percent_2", "minutes_2"]


class ValueRef(StrictDTO):
    """
    作用：定位冻结结果中的单元格
    输入：产物 ID、零基行号和原始列名
    输出：严格引用
    """

    artifact_id: str
    row: int = Field(ge=0, strict=True)
    column: str = Field(min_length=1, max_length=200)


class KPIBlock(StrictDTO):
    """
    作用：声明关键数值卡片
    输入：标签、取值引用和显示格式
    输出：待校验卡片
    """

    type: Literal["kpi"]
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    value_ref: ValueRef
    format: ValueFormat


class TableBlock(StrictDTO):
    """
    作用：声明冻结结果表格
    输入：产物、原始列及可选中文列名
    输出：待校验表格
    """

    type: Literal["table"]
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    artifact_id: str
    columns: list[str] = Field(min_length=1, max_length=16)
    column_labels: dict[str, str] = Field(default_factory=dict, max_length=16)


class ChartBlock(StrictDTO):
    """
    作用：限定图表种类与真实数据列
    输入：柱状或折线图、数据列和中文标签
    输出：不含可执行配置的图表声明
    """

    type: Literal["chart"]
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    artifact_id: str
    kind: Literal["bar", "line"]
    x: str
    y: list[str] = Field(min_length=1, max_length=3)
    x_label: str = Field(default="分类", max_length=120)
    series_labels: dict[str, str] = Field(default_factory=dict, max_length=3)
    format: ValueFormat = "decimal_2"


class RowBinding(StrictDTO):
    """
    作用：声明同一结果行上的分组身份和数值
    输入：文字占位键列表
    输出：待校验的同行绑定
    """

    dimensions: list[str] = Field(min_length=1, max_length=8)
    measures: list[str] = Field(min_length=1, max_length=8)


class TextBlock(StrictDTO):
    """
    作用：让文字中的数量通过受限占位符取值
    输入：纯文本模板、单元格引用及证据 ID
    输出：待物化说明
    """

    type: Literal["text"]
    id: str = Field(min_length=1, max_length=80)
    text_template: str = Field(min_length=1, max_length=2400)
    values: dict[str, ValueRef] = Field(default_factory=dict, max_length=24)
    evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    row_bindings: list[RowBinding] = Field(default_factory=list, max_length=24)


Block = Annotated[KPIBlock | TableBlock | ChartBlock | TextBlock, Field(discriminator="type")]


class ReportDraft(StrictDTO):
    """
    作用：约束模型报告草稿，不接受模型填写数值或验证等级
    输入：范围、指标版本、布局和限制
    输出：严格报告候选
    """

    schema_version: Literal["1"]
    title: str = Field(min_length=1, max_length=160)
    scope: str = Field(min_length=1, max_length=1000)
    metric_revision_ids: list[str] = Field(default_factory=list, max_length=16)
    blocks: list[Block] = Field(min_length=1, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=12)


class ReportCandidate(StrictDTO):
    """
    作用：接收模型的报告最终候选
    输入：报告草稿
    输出：交由服务端校验的候选
    """

    kind: Literal["report"]
    report: ReportDraft


class ReportResult(StrictDTO):
    """
    作用：让运行结果仅引用已提交的报告
    输入：服务端报告和修订 ID
    输出：持久化运行结果
    """

    kind: Literal["report"]
    report_id: str
    revision_id: str


class FrozenValue(StrictDTO):
    """
    作用：公开服务端读取和格式化的真实单元格
    输入：原始值、显示值与引用
    输出：带证据入口的单元格
    """

    raw: Any
    display: str
    value_ref: ValueRef


class ReportColumn(StrictDTO):
    """
    作用：保留真实列名与中文显示名
    输入：列键和标题
    输出：表格列
    """

    name: str
    label: str


class TextSegment(StrictDTO):
    """
    作用：区分普通文字与可点击的服务端取值
    输入：文字或有命名键的单元格
    输出：安全文字片段
    """

    text: str
    key: str | None = None
    value: FrozenValue | None = None


class RenderedBlock(StrictDTO):
    """
    作用：提供由服务端物化的统一块 DTO
    输入：已校验块与冻结数据
    输出：只读展示数据
    """

    type: Literal["kpi", "table", "chart", "text"]
    id: str
    title: str = ""
    value: FrozenValue | None = None
    artifact_id: str | None = None
    columns: list[ReportColumn] = Field(default_factory=list)
    rows: list[list[FrozenValue]] = Field(default_factory=list)
    kind: Literal["bar", "line"] | None = None
    segments: list[TextSegment] = Field(default_factory=list)
    row_bindings: list[RowBinding] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    ordered: bool = False


class FrozenSource(StrictDTO):
    """
    作用：保存固定原文或知识版本的可读副本
    输入：来源类型、标题、原文与定位
    输出：不依赖当前版本的来源
    """

    id: str
    revision_id: str
    title: str
    source_kind: str
    content: str
    locator: dict[str, Any] = Field(default_factory=dict)


class ReportSummary(StrictDTO):
    """
    作用：展示实际已提交报告列表
    输入：服务端报告记录
    输出：摘要卡片
    """

    id: str
    revision_id: str
    version: int
    title: str
    question: str
    scope: str
    period: str | None
    date_range: DateRange | None = None
    source_kind: str
    created_at: str


class ReportDTO(StrictDTO):
    """
    作用：提供不可变报告正文及固定依据
    输入：服务端冻结修订
    输出：报告详情 DTO
    """

    report_id: str
    revision_id: str
    version: int
    run_id: str
    question: str
    created_at: str
    title: str
    scope: str
    limitations: list[str]
    validation_level: Literal["references_validated"] = "references_validated"
    data_snapshot: SnapshotDTO
    knowledge_snapshot_id: str
    blocks: list[RenderedBlock]
    sources: list[FrozenSource]
    metrics: list[dict[str, Any]]
    draft: ReportDraft
    content_hash: str
    external_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    date_range: DateRange | None = None


class EvidenceDTO(StrictDTO):
    """
    作用：只从指定报告的冻结索引返回依据
    输入：报告块和受限单元格选择
    输出：原值、执行记录、固定来源与口径
    """

    block_id: str
    value: FrozenValue | None = None
    artifact: QueryArtifact | None = None
    data_snapshot: SnapshotDTO
    knowledge_snapshot_id: str
    sources: list[FrozenSource]
    metrics: list[dict[str, Any]]
    validation_level: Literal["references_validated"] = "references_validated"
    external_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    date_range: DateRange | None = None


class ReportRevisionWrite(StrictDTO):
    """
    作用：显式修订同一批冻结证据上的报告
    输入：加载时版本和新草稿
    输出：带并发保护的修订请求
    """

    expected_version: int = Field(ge=1)
    report: ReportDraft
