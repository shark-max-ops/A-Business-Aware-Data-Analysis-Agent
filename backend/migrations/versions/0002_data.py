"""固定数据源、快照和查询证据。"""

import sqlalchemy as sa
from alembic import op

revision = "0002_data"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：建立阶段 1 元数据表和索引
    输入：无
    输出：无
    """
    op.create_table(
        "data_sources",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False, unique=True),
        sa.Column("current_snapshot_id", sa.String(), nullable=True),
    )
    op.create_table(
        "data_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_id", sa.String(), sa.ForeignKey("data_sources.id"), nullable=False),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("file_hash", sa.String(), nullable=False),
        sa.Column("schema_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("quality_report", sa.JSON(), nullable=False),
        sa.Column("schema", sa.JSON(), nullable=False),
    )
    op.create_index("ix_data_snapshots_source_id", "data_snapshots", ["source_id"])
    op.create_table(
        "query_executions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("sql", sa.String(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
    )
    op.create_index("ix_query_executions_run_id", "query_executions", ["run_id"])
    op.create_table(
        "query_artifacts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.String(),
            sa.ForeignKey("query_executions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), sa.ForeignKey("data_snapshots.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_query_artifacts_run_id", "query_artifacts", ["run_id"])


def downgrade():
    """
    作用：撤销阶段 1 表，不删除任何快照文件
    输入：无
    输出：无
    """
    for table in ["query_artifacts", "query_executions", "data_snapshots", "data_sources"]:
        op.drop_table(table)
