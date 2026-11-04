"""追加外部证据、天气快照和运行版本授权；保留既有主快照校验。"""

import sqlalchemy as sa
from alembic import op

revision = "0007_external_evidence"
down_revision = "0006_investigations"
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：新增独立外部证据及显式查询快照绑定。
    输入：无。
    输出：无。
    """
    op.create_table(
        "external_evidence",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("owner_run_id", sa.String, sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("slot", sa.String, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_table(
        "external_snapshots",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "evidence_id",
            sa.String,
            sa.ForeignKey("external_evidence.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
    )
    op.create_table(
        "external_run_bindings",
        sa.Column("run_id", sa.String, sa.ForeignKey("agent_runs.id"), primary_key=True),
        sa.Column(
            "evidence_id", sa.String, sa.ForeignKey("external_evidence.id"), primary_key=True
        ),
        sa.Column("slot", sa.String, nullable=False),
        sa.UniqueConstraint("run_id", "slot", name="uq_external_run_slot"),
    )
    # SQLite 原地增加可空引用列，避免重建已有父表时破坏查询产物外键。
    op.execute(
        "ALTER TABLE query_executions ADD COLUMN external_snapshot_id VARCHAR "
        "CONSTRAINT fk_query_external_snapshot REFERENCES external_snapshots(id)"
    )


def downgrade():
    """
    作用：撤销外部元数据结构，不删除原件、报告或固定数据库文件。
    输入：无。
    输出：无。
    """
    op.execute("ALTER TABLE query_executions DROP COLUMN external_snapshot_id")
    op.drop_table("external_run_bindings")
    op.drop_table("external_snapshots")
    op.drop_table("external_evidence")
