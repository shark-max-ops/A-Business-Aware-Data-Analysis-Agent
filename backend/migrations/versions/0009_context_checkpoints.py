"""Run-local context checkpoints; no execution recovery."""

import sqlalchemy as sa
from alembic import op

revision = "0009_context_checkpoints"
down_revision = "0008_multimonth"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "run_context_checkpoints",
        sa.Column("run_id", sa.String, primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("previous_revision", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
    )


def downgrade():
    op.drop_table("run_context_checkpoints")
