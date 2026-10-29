"""阶段 1 元数据；业务数据仍只保存在 DuckDB 快照。"""

from typing import Any

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from metric_pilot.database import Base


class DataSource(Base):
    """
    作用：登记 Reporting Carrier 数据来源，隔离合成与官方数据
    输入：服务端标识、类型和当前版本
    输出：数据源记录
    """

    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    source_kind: Mapped[str] = mapped_column(unique=True)
    current_snapshot_id: Mapped[str | None]


class DataSnapshot(Base):
    """
    作用：登记不可覆盖的数据版本
    输入：清单、质量、结构和哈希
    输出：快照记录
    """

    __tablename__ = "data_snapshots"
    id: Mapped[str] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    period: Mapped[str]
    row_count: Mapped[int]
    file_hash: Mapped[str]
    schema_hash: Mapped[str]
    created_at: Mapped[str]
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSON)
    schema: Mapped[dict[str, Any]] = mapped_column(JSON)


class QueryExecution(Base):
    """
    作用：记录全部查询尝试，失败不会生成成功产物
    输入：运行、绑定快照、执行状态和安全错误码
    输出：执行审计记录
    """

    __tablename__ = "query_executions"
    id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"))
    status: Mapped[str]
    error_code: Mapped[str | None]
    created_at: Mapped[str]
    duration_ms: Mapped[int] = mapped_column(default=0)
    sql: Mapped[str]
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)


class ArtifactRecord(Base):
    """
    作用：索引已成功执行并冻结的查询产物
    输入：产物标识、执行关联与元数据
    输出：证据索引记录
    """

    __tablename__ = "query_artifacts"
    id: Mapped[str] = mapped_column(primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("query_executions.id"), unique=True)
    run_id: Mapped[str] = mapped_column(index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("data_snapshots.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
