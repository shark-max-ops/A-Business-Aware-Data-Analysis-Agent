"""外部证据、独立天气快照和逐运行版本绑定；不作为主数据源注册。"""

import sqlalchemy as sa

from metric_pilot.database import Base

evidence = sa.Table(
    "external_evidence",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("owner_run_id", sa.String, sa.ForeignKey("agent_runs.id"), nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("slot", sa.String, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("content_hash", sa.String, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
)
snapshots = sa.Table(
    "external_snapshots",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("evidence_id", sa.String, sa.ForeignKey(evidence.c.id), nullable=False, unique=True),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("content_hash", sa.String, nullable=False),
)
bindings = sa.Table(
    "external_run_bindings",
    Base.metadata,
    sa.Column("run_id", sa.String, sa.ForeignKey("agent_runs.id"), primary_key=True),
    sa.Column("evidence_id", sa.String, sa.ForeignKey(evidence.c.id), primary_key=True),
    sa.Column("slot", sa.String, nullable=False),
    sa.UniqueConstraint("run_id", "slot", name="uq_external_run_slot"),
)
