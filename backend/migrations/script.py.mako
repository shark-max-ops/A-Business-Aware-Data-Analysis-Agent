"""${message}"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    """
    作用：应用本次元数据变更
    输入：无
    输出：无
    """
    ${upgrades if upgrades else "pass"}


def downgrade():
    """
    作用：撤销本次元数据变更
    输入：无
    输出：无
    """
    ${downgrades if downgrades else "pass"}
