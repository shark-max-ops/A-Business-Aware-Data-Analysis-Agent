"""按需为旧会话补短标题；默认只预览，--apply 才请求已配置模型。"""

import argparse
import asyncio
import json

from metric_pilot.agent import models as m
from metric_pilot.agent.store import RunStore
from metric_pilot.agent.titles import generate_title
from metric_pilot.data.common import DataError, utc_now
from metric_pilot.database import create_metadata_engine
from metric_pilot.settings import Settings
from sqlalchemy import select


async def main():
    """
    作用：显式补全旧会话标题并记录实际请求结果
    输入：命令行的应用开关及数量上限
    输出：安全摘要和本地验收记录
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    settings = Settings()
    engine = create_metadata_engine(settings)
    store = RunStore(engine)
    candidates = []
    # 仅处理最新问题的旧自动标题，跳过自定义名、短名和仍在执行的会话。
    with engine.connect() as connection:
        for conv in connection.execute(select(m.conversations)).mappings():
            latest = (
                connection.execute(
                    select(m.runs)
                    .where(m.runs.c.conversation_id == conv["id"])
                    .order_by(m.runs.c.payload["created_at"].as_string().desc(), m.runs.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if not latest or latest["status"] in {"queued", "running"}:
                continue
            question = latest["payload"]["question"]
            if len(conv["title"]) > 10 and conv["title"] in {
                question[:60],
                " ".join(question.split()),
            }:
                candidates.append((dict(conv), dict(latest)))
    records = []
    try:
        for conv, run in candidates[: max(0, args.limit)]:
            record = {"conversation_id": conv["id"], "status": "preview"}
            if args.apply:
                try:
                    title, usage, attempts = await generate_title(
                        settings, run["payload"]["question"]
                    )
                    with store.write() as connection:
                        applied = store.apply_title(connection, run, title, conv["title"])
                    record.update(
                        status="updated" if applied else "changed_skipped",
                        title=title,
                        usage=usage,
                        attempts=attempts,
                    )
                except DataError as error:
                    record.update(status="failed", error_code=error.code)
                except Exception:
                    record.update(status="failed", error_code="TITLE_REQUEST_FAILED")
            records.append(record)
        report = {
            "created_at": utc_now(),
            "apply": args.apply,
            "candidates": len(candidates),
            "items": records,
        }
        if args.apply:
            target = settings.storage_dir / "validation" / "conversation-titles.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
