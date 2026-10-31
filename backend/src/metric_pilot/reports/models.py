"""报告与不可变修订元数据。"""

import sqlalchemy as sa

from metric_pilot.agent.models import runs
from metric_pilot.database import Base

reports = sa.Table(
    "reports",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("run_id", sa.String, sa.ForeignKey(runs.c.id), unique=True, nullable=False),
    sa.Column("latest_revision_id", sa.String, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
)
revisions = sa.Table(
    "report_revisions",
    Base.metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("report_id", sa.String, sa.ForeignKey(reports.c.id), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("content_hash", sa.String, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.UniqueConstraint("report_id", "version"),
)
