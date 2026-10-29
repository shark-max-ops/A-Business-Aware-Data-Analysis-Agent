"""导出无需启动服务器和数据库的 API 契约。"""

import json
from pathlib import Path

from metric_pilot.main import create_app
from metric_pilot.settings import Settings

# 固定空配置，契约生成不会读取用户的模型凭据。
schema = create_app(Settings(_env_file=None)).openapi()
target = Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
target.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
