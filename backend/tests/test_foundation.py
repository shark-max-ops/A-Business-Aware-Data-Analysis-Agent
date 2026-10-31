"""阶段 0 的迁移、健康与脱敏验收。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from metric_pilot.database import create_metadata_engine
from metric_pilot.main import create_app
from metric_pilot.settings import Settings
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]


def migrate(monkeypatch, directory, revision="head"):
    """
    作用：在隔离目录执行 Alembic 迁移
    输入：环境隔离、存储目录和目标版本
    输出：无
    """
    monkeypatch.setenv("STORAGE_DIR", str(directory))
    command.upgrade(Config(str(ROOT / "alembic.ini")), revision)


def settings(directory, **overrides):
    """
    作用：创建不读取本机密钥的配置
    输入：存储目录和配置覆盖
    输出：Settings
    """
    values = dict(
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
    )
    values.update(overrides)
    return Settings(_env_file=None, storage_dir=directory, **values)


def test_migrations_and_sqlite_policy(tmp_path, monkeypatch):
    """
    作用：验证迁移可重复、可回退以及 SQLite 连接策略
    输入：临时目录和环境隔离
    输出：无
    """
    migrate(monkeypatch, tmp_path)
    migrate(monkeypatch, tmp_path)
    engine = create_metadata_engine(settings(tmp_path))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM app_settings")).scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
    engine.dispose()
    command.downgrade(Config(str(ROOT / "alembic.ini")), "base")
    migrate(monkeypatch, tmp_path)
    command.check(Config(str(ROOT / "alembic.ini")))


def test_health_without_model_key(tmp_path, monkeypatch):
    """
    作用：验证无密钥服务可用和 FTS5 支持
    输入：临时目录和环境隔离
    输出：无
    """
    migrate(monkeypatch, tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["database"] == "ready"
        assert body["fts5"] == "available"
        assert body["llm"] == "not_configured"
        assert body["worker"] == "ready"
        assert body["analysis"] == "ready"
        assert response.headers["x-request-id"]


def test_missing_migration_is_not_healthy(tmp_path):
    """
    作用：验证未迁移数据库不会显示健康
    输入：临时目录
    输出：无
    """
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 503
        assert response.json()["database"] == "migration_required"


def test_credentials_are_not_exposed_or_called(tmp_path, monkeypatch):
    """
    作用：验证配置不触发远程连接且不泄露秘密
    输入：临时目录和环境隔离
    输出：无
    """
    migrate(monkeypatch, tmp_path)
    config = settings(
        tmp_path,
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="test-private-secret",
        llm_model="test-model",
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["llm"] == "configured_unverified"
        assert "test-private-secret" not in response.text
        assert str(tmp_path) not in response.text
        assert "127.0.0.1:1" not in response.text
    example = Settings(_env_file=ROOT / ".env.example")
    assert not example.integration_configured("llm")
    assert not example.integration_configured("embedding")


def test_error_envelope_and_redaction(tmp_path):
    """
    作用：验证错误响应一致且排除原始敏感内容
    输入：临时目录
    输出：无
    """
    app = create_app(settings(tmp_path))

    @app.get("/test/validation")
    def validated(count: int):
        """
        作用：验证隔离环境中的应用行为
        输入：测试参数与临时目录
        输出：测试结果或配置对象
        """
        return count

    @app.get("/test/failure")
    def failing():
        """
        作用：验证隔离环境中的应用行为
        输入：测试参数与临时目录
        输出：测试结果或配置对象
        """
        raise RuntimeError("private-secret-and-local-path")

    with TestClient(app, raise_server_exceptions=False) as client:
        for url, status in [
            ("/missing", 404),
            ("/test/validation?count=secret-input", 422),
            ("/test/failure", 500),
        ]:
            response = client.get(url)
            assert response.status_code == status
            error = response.json()["error"]
            assert set(error) == {"code", "message", "details", "request_id"}
            assert error["request_id"] == response.headers["x-request-id"]
            assert "secret-input" not in response.text
            assert "private-secret-and-local-path" not in response.text
