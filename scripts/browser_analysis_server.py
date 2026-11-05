"""仅供阶段 3 浏览器测试的隔离启动器；正式服务无 FakeLLM 开关。"""

import os
import sys
from pathlib import Path
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from metric_pilot.data.catalog import Catalog
from metric_pilot.data.ingest import ingest
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.main import create_app
from metric_pilot.settings import Settings

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "backend" / "tests"))
    import metric_pilot.agent.runtime as runtime
    from browser_agent_model import BrowserModel
    from browser_external_sources import install
    from test_knowledge import add_document

    directory = root / ".cache" / "browser-analysis" / uuid4().hex
    os.environ["STORAGE_DIR"] = str(directory)
    command.upgrade(Config(str(root / "alembic.ini")), "head")
    config = Settings(
        _env_file=None,
        storage_dir=directory,
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="isolated-test",
        llm_model="isolated-test",
        embedding_base_url="",
        embedding_api_key="",
        embedding_model="",
    )
    snapshot = ingest(
        config, root / "fixtures/synthetic_flight_records.csv", "2026-01", synthetic=True
    )
    engine = create_metadata_engine(config)
    Catalog(engine, directory).activate(snapshot.source_id, snapshot.id)
    add_document(KnowledgeStore(engine))
    install(directory)
    engine.dispose()
    runtime.AgentProvider = BrowserModel
    uvicorn.run(create_app(config), host="127.0.0.1", port=8002)
