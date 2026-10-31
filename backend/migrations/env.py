"""从与服务相同的配置加载 SQLite 迁移目标。"""

from alembic import context
from metric_pilot.agent import models as agent_models  # noqa: F401
from metric_pilot.data import models  # noqa: F401
from metric_pilot.database import Base, create_metadata_engine
from metric_pilot.knowledge import models as knowledge_models  # noqa: F401
from metric_pilot.reports import models as report_models  # noqa: F401
from metric_pilot.settings import Settings

# 迁移与应用共用引擎，避免两个独立配置指向不同数据库。
engine = create_metadata_engine(Settings())


def include_object(obj, name, type_, reflected, compare_to):
    """
    作用：排除由迁移显式管理的 FTS5 虚表及内部影子表
    输入：Alembic 对象与比较信息
    输出：是否纳入 ORM 结构比较
    """
    return not (type_ == "table" and name.startswith("knowledge_fts"))


try:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
