"""SQLite 连接与阶段 0 数据库自检。"""

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from metric_pilot.settings import Settings

SCHEMA_REVISION = "0006_investigations"


class Base(DeclarativeBase):
    """
    作用：提供应用元数据的 SQLAlchemy 映射基类
    输入：无
    输出：模型元数据
    """


class AppSetting(Base):
    """
    作用：持久化本地应用设置，不存储模型密钥
    输入：设置键和值
    输出：应用设置记录
    """

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


def create_metadata_engine(settings: Settings) -> Engine:
    """
    作用：建立工作区元数据库引擎并应用 SQLite 连接策略
    输入：settings - 应用配置
    输出：SQLAlchemy Engine
    """
    # 业务数据和密钥不会写入应用设置表。
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite", database=str(settings.storage_dir.resolve() / "metadata.sqlite")),
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record):
        """
        作用：为每条连接开启外键、WAL 和锁等待
        输入：connection - DBAPI 连接；_record - 连接池记录
        输出：无
        """
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


def inspect_database(engine: Engine) -> tuple[str, str]:
    """
    作用：检查迁移版本、实际表和 FTS5 支持
    输入：engine - 元数据库引擎
    输出：数据库状态和 FTS5 状态
    """
    database, fts5 = "unavailable", "unavailable"
    try:
        with engine.connect() as connection:
            # 临时表只用于能力检测，不生成业务内容。
            try:
                connection.exec_driver_sql("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(body)")
                connection.exec_driver_sql("DROP TABLE temp.fts_probe")
                fts5 = "available"
            except SQLAlchemyError:
                pass
            try:
                revisions = connection.execute(text("SELECT version_num FROM alembic_version"))
                ready = set(revisions.scalars()) == {SCHEMA_REVISION}
                connection.execute(text("SELECT key, value FROM app_settings LIMIT 0"))
                for table in (
                    "data_sources",
                    "data_snapshots",
                    "query_executions",
                    "query_artifacts",
                    "knowledge_documents",
                    "document_revisions",
                    "knowledge_chunks",
                    "knowledge_jobs",
                    "wiki_pages",
                    "conversations",
                    "agent_runs",
                    "tool_executions",
                    "reports",
                    "report_revisions",
                    "investigations",
                    "investigation_findings",
                ):
                    connection.execute(text(f"SELECT id FROM {table} LIMIT 0"))
                connection.execute(text("SELECT key FROM knowledge_metrics LIMIT 0"))
                connection.execute(text("SELECT chunk_id FROM knowledge_fts LIMIT 0"))
                database = "ready" if ready else "migration_required"
            except SQLAlchemyError:
                database = "migration_required"
    except SQLAlchemyError:
        pass
    return database, fts5
