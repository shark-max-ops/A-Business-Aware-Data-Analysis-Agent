"""保存调查边界、累计额度、运行关联与不可变发现。"""

import sqlalchemy as sa
from alembic import op

revision = "0006_investigations"
down_revision = "0005_reports"
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：新增调查表，保留原有运行与报告
    输入：无
    输出：无
    """
    op.create_table(
        "investigations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("conversation_id", sa.String, sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
    )
    op.create_table(
        "investigation_runs",
        sa.Column("run_id", sa.String, sa.ForeignKey("agent_runs.id"), primary_key=True),
        sa.Column(
            "investigation_id", sa.String, sa.ForeignKey("investigations.id"), nullable=False
        ),
    )
    op.create_index(
        "ix_investigation_runs_investigation_id", "investigation_runs", ["investigation_id"]
    )
    op.create_table(
        "investigation_findings",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "investigation_id", sa.String, sa.ForeignKey("investigations.id"), nullable=False
        ),
        sa.Column("run_id", sa.String, sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
    )


def downgrade():
    """
    作用：撤销调查元数据表
    输入：无
    输出：无
    """
    op.drop_table("investigation_findings")
    op.drop_index("ix_investigation_runs_investigation_id", "investigation_runs")
    op.drop_table("investigation_runs")
    op.drop_table("investigations")
