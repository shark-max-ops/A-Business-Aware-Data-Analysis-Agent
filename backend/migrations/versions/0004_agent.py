"""冻结阶段 3 迁移；不依赖运行时模型。"""

from alembic import op

revision = "0004_agent"
down_revision = "0003_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：建立运行与事件表
    输入：无
    输出：无
    """
    op.execute("""CREATE TABLE conversations (
        id VARCHAR PRIMARY KEY NOT NULL, title VARCHAR NOT NULL, created_at VARCHAR NOT NULL)""")
    op.execute("""CREATE TABLE agent_runs (
        id VARCHAR PRIMARY KEY NOT NULL,
        conversation_id VARCHAR NOT NULL REFERENCES conversations(id),
        status VARCHAR NOT NULL, payload JSON NOT NULL, seq INTEGER NOT NULL,
        cancel_requested BOOLEAN NOT NULL)""")
    op.execute("CREATE INDEX ix_agent_runs_status ON agent_runs(status)")
    op.execute("""CREATE TABLE run_events (
        run_id VARCHAR NOT NULL REFERENCES agent_runs(id), seq INTEGER NOT NULL,
        type VARCHAR NOT NULL, created_at VARCHAR NOT NULL, payload JSON NOT NULL,
        PRIMARY KEY (run_id, seq))""")
    op.execute("""CREATE TABLE tool_executions (
        id VARCHAR PRIMARY KEY NOT NULL, run_id VARCHAR NOT NULL REFERENCES agent_runs(id),
        tool_call_id VARCHAR NOT NULL, name VARCHAR NOT NULL, arguments JSON NOT NULL,
        status VARCHAR NOT NULL, result JSON NOT NULL, duration_ms INTEGER NOT NULL,
        UNIQUE(run_id, tool_call_id))""")


def downgrade():
    """
    作用：撤销阶段 3 元数据
    输入：无
    输出：无
    """
    for name in ("tool_executions", "run_events", "agent_runs", "conversations"):
        op.drop_table(name)
