"""阶段 1 数据与查询契约；公共字段遵循蓝图 02、03、05、06。"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from metric_pilot.contracts import StrictDTO


@dataclass(frozen=True)
class RunContext:
    """
    作用：由服务端固定查询所属运行和快照
    输入：运行、数据和知识快照标识及单调时钟截止时间
    输出：不可变执行上下文
    """

    run_id: str
    snapshot_id: str
    knowledge_snapshot_id: str
    deadline_monotonic: float


class ToolResult(StrictDTO):
    """
    作用：统一工具执行反馈
    输入：状态、摘要、数据、产物和可恢复错误
    输出：工具结果
    """

    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False


class Page[T](StrictDTO):
    """
    作用：统一列表响应
    输入：列表和后续游标
    输出：列表 DTO
    """

    items: list[T]
    next_cursor: str | None = None


class SourceDTO(StrictDTO):
    """
    作用：描述已登记数据源
    输入：来源身份和当前快照
    输出：不含本机路径的数据源
    """

    id: str
    name: str
    adapter: Literal["duckdb"] = "duckdb"
    source_kind: Literal["official_bts", "synthetic"]
    current_snapshot_id: str | None
    status: Literal["ready"] = "ready"


class SnapshotDTO(StrictDTO):
    """
    作用：描述固定数据与导入质量
    输入：服务端计算的版本、清单和质量摘要
    输出：快照 DTO
    """

    id: str
    source_id: str
    period: str | None
    periods: list[str] = Field(default_factory=list)
    coverage_start: str | None = None
    coverage_end_exclusive: str | None = None
    row_count: int
    file_hash: str
    schema_hash: str
    created_at: str
    manifest: dict[str, Any]
    quality_report: dict[str, Any]


class ColumnDTO(StrictDTO):
    """
    作用：描述结果或视图列
    输入：名称和 DuckDB 类型
    输出：列 DTO
    """

    name: str
    type: str
    description: str = ""


class TableDTO(StrictDTO):
    """
    作用：公开允许查询的固定业务结构
    输入：视图名、列、样本和关联键
    输出：表结构 DTO
    """

    name: str
    columns: list[ColumnDTO]
    sample: list[dict[str, Any]]
    join_keys: list[str] = Field(default_factory=list)


class SchemaDTO(StrictDTO):
    """
    作用：返回某个固定快照的公开结构
    输入：快照标识与表结构
    输出：结构 DTO
    """

    snapshot_id: str
    tables: list[TableDTO]


class ActivateRequest(StrictDTO):
    """
    作用：显式选择默认快照
    输入：快照标识
    输出：校验后的请求
    """

    snapshot_id: str


class ExplorationScope(StrictDTO):
    """
    作用：记录探索查询声明的范围、算法与缺失处理，不代表业务语义已审核
    输入：模型随查询提交的简短说明
    输出：随成功产物冻结的探索说明
    """

    population: str = Field(min_length=1, max_length=600, pattern=r"\S")
    calculation: str = Field(min_length=1, max_length=600, pattern=r"\S")
    missing_values: str = Field(min_length=1, max_length=600, pattern=r"\S")


class QueryArtifact(StrictDTO):
    """
    作用：保存成功执行的不可变证据元数据
    输入：服务端执行信息、快照、结果哈希和规则引用
    输出：蓝图 05 查询产物
    """

    id: str
    run_id: str
    execution_id: str
    snapshot_id: str
    sql: str
    parameters: dict[str, Any]
    guard_version: str
    normalized_sql: str
    executed_sql: str
    parameter_keys: list[str]
    tables: list[str]
    functions: list[str]
    columns: list[ColumnDTO]
    returned_rows: int
    truncated: bool
    result_hash: str
    metric_revision_ids: list[str] = Field(default_factory=list)
    knowledge_revision_ids: list[str] = Field(default_factory=list)
    query_purpose: Literal["legacy", "exploratory", "reviewed_metric"] = "legacy"
    exploration_scope: ExplorationScope | None = None
    unavailable_metric_keys: list[str] = Field(default_factory=list)
    duration_ms: int
    created_at: str
    ordered: bool
    external_snapshot_id: str | None = None
    external_evidence_ids: list[str] = Field(default_factory=list)


class ArtifactPage(StrictDTO):
    """
    作用：返回冻结产物的有限分页，不执行原查询
    输入：产物元数据、行和分页位置
    输出：产物读取 DTO
    """

    artifact: QueryArtifact
    rows: list[list[Any]]
    offset: int
    next_offset: int | None
