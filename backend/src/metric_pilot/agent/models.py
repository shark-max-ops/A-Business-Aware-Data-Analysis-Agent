"""阶段 3 元数据映射；Run 本身是持久化的串行分析任务。"""

import sqlalchemy as sa

from metric_pilot.database import Base

conversations = sa.Table(
    "conversations",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("title", sa.String, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
)
runs = sa.Table(
    "agent_runs",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("conversation_id", sa.String, sa.ForeignKey(conversations.c.id), nullable=False),
    sa.Column("status", sa.String, nullable=False, index=True),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("cancel_requested", sa.Boolean, nullable=False),
)
events = sa.Table(
    "run_events",
    Base.metadata,
    sa.Column("run_id", sa.String, sa.ForeignKey(runs.c.id), primary_key=True),
    sa.Column("seq", sa.Integer, primary_key=True),
    sa.Column("type", sa.String, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
)
executions = sa.Table(
    "tool_executions",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, sa.ForeignKey(runs.c.id), nullable=False),
    sa.Column("tool_call_id", sa.String, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("arguments", sa.JSON, nullable=False),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("result", sa.JSON, nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=False),
    sa.UniqueConstraint("run_id", "tool_call_id"),
)

# 调查边界和运行关联单独保存，不改变历史运行的归属。
investigations = sa.Table(
    "investigations",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("conversation_id", sa.String, sa.ForeignKey(conversations.c.id), nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
)
investigation_runs = sa.Table(
    "investigation_runs",
    Base.metadata,
    sa.Column("run_id", sa.String, sa.ForeignKey(runs.c.id), primary_key=True),
    sa.Column(
        "investigation_id",
        sa.String,
        sa.ForeignKey(investigations.c.id),
        nullable=False,
        index=True,
    ),
)
findings = sa.Table(
    "investigation_findings",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("investigation_id", sa.String, sa.ForeignKey(investigations.c.id), nullable=False),
    sa.Column("run_id", sa.String, sa.ForeignKey(runs.c.id), nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("content_hash", sa.String, nullable=False),
)
