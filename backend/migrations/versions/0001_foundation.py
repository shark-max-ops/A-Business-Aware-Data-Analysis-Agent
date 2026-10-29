"""阶段 0 应用设置表；不预填业务数据。"""

import sqlalchemy as sa
from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：创建本地应用设置表
    输入：无
    输出：无
    """
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )


def downgrade():
    """
    作用：撤销阶段 0 设置表
    输入：无
    输出：无
    """
    op.drop_table("app_settings")
