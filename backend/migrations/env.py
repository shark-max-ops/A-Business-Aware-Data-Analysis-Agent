"""从与服务相同的配置加载 SQLite 迁移目标。"""

from alembic import context
from metric_pilot.data import models  # noqa: F401
from metric_pilot.database import Base, create_metadata_engine
from metric_pilot.settings import Settings

# 迁移与应用共用引擎，避免两个独立配置指向不同数据库。
engine = create_metadata_engine(Settings())
try:
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
