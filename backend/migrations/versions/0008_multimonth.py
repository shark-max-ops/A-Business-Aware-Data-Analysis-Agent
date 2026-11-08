"""允许多月快照不填写单月period；范围另存在版本化清单中。"""

import sqlalchemy as sa
from alembic import op

revision = "0008_multimonth"
down_revision = "0007_external_evidence"
branch_labels = None
depends_on = None


def upgrade():
    """
    作用：使单月字段可空，不把日期范围编码进单月字符串
    输入：无
    输出：无，旧行值保持不变
    """
    with op.batch_alter_table("data_snapshots") as batch:
        batch.alter_column("period", existing_type=sa.String(), nullable=True)


def downgrade():
    """
    作用：仅在没有多月快照时撤回可空字段，禁止删除多月数据以降级
    输入：无
    输出：无，多月记录存在时拒绝降级
    """
    if (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM data_snapshots WHERE period IS NULL"))
        .scalar_one()
    ):
        raise RuntimeError("MULTIMONTH_SNAPSHOTS_PREVENT_DOWNGRADE")
    with op.batch_alter_table("data_snapshots") as batch:
        batch.alter_column("period", existing_type=sa.String(), nullable=False)
