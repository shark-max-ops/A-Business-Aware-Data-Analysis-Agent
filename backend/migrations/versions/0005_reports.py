"""冻结阶段 4 报告迁移，不依赖运行时模型。"""

from alembic import op

revision = "0005_reports"
down_revision = "0004_agent"
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：建立报告与不可变修订表
    输入：无
    输出：无
    """
    op.execute("""CREATE TABLE reports (
        id VARCHAR PRIMARY KEY NOT NULL,
        run_id VARCHAR NOT NULL UNIQUE REFERENCES agent_runs(id),
        latest_revision_id VARCHAR NOT NULL, version INTEGER NOT NULL,
        created_at VARCHAR NOT NULL)""")
    op.execute("""CREATE TABLE report_revisions (
        id VARCHAR PRIMARY KEY NOT NULL, report_id VARCHAR NOT NULL REFERENCES reports(id),
        version INTEGER NOT NULL, payload JSON NOT NULL, content_hash VARCHAR NOT NULL,
        created_at VARCHAR NOT NULL, UNIQUE(report_id, version))""")


def downgrade():
    """
    作用：撤销阶段 4 表，不影响已有数据与运行表
    输入：无
    输出：无
    """
    op.drop_table("report_revisions")
    op.drop_table("reports")
