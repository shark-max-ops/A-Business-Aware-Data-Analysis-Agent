"""仅供浏览器验收的隔离服务；不读取模型配置或真实工作区数据库。"""

import os
from pathlib import Path
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config
from metric_pilot.main import create_app
from metric_pilot.settings import Settings

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    directory = root / ".cache" / "browser-knowledge" / uuid4().hex
    os.environ["STORAGE_DIR"] = str(directory)
    command.upgrade(Config(str(root / "alembic.ini")), "head")
    config = Settings(
        _env_file=None,
        storage_dir=directory,
        llm_base_url="",
        llm_model="",
        llm_api_key="",
        embedding_base_url="",
        embedding_model="",
        embedding_api_key="",
    )
    uvicorn.run(create_app(config), host="127.0.0.1", port=8001)
