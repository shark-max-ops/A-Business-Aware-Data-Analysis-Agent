"""核验本地材料清单并导入文档与指标草稿；不会自动审核。"""

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from metric_pilot.data.common import DataError
from metric_pilot.database import create_metadata_engine, inspect_database
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.contracts import DocumentImport, DocumentMetadata, MetricWrite
from metric_pilot.knowledge.documents import DocumentService
from metric_pilot.knowledge.jobs import KnowledgeWorker
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.settings import Settings


def verified_path(root, path, expected_hash):
    """
    作用：仅接受工作区内且字节哈希匹配的准备文件
    输入：根目录、相对路径与预期哈希
    输出：已核验路径
    """
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise DataError("SEED_FILE_UNAVAILABLE")
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
        raise DataError("SEED_HASH_MISMATCH", {"material": "manifest input"})
    return target


def import_seed(settings, manifest_path, embed=False):
    """
    作用：按实际材料绑定正式文档修订，指标仍保持草稿
    输入：配置、本地清单、是否显式构建向量
    输出：导入统计与知识快照
    """
    root = Path.cwd().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 先验证全部文件，避免中途发现明显损坏才留下半份导入。
    for doc in manifest["documents"]:
        verified_path(root, doc["path"], doc["content_hash"])
        for raw in doc.get("raw_sources", []):
            verified_path(root, raw["path"], raw["sha256"])
    metric_package = json.loads(
        verified_path(
            root, manifest["metrics"]["path"], manifest["metrics"]["content_hash"]
        ).read_text(encoding="utf-8")
    )
    engine = create_metadata_engine(settings)
    try:
        if inspect_database(engine) != ("ready", "available"):
            raise DataError("MIGRATION_REQUIRED")
        store = KnowledgeStore(engine)
        documents = DocumentService(store)
        bindings = {}
        for doc in manifest["documents"]:
            with engine.connect() as connection:
                current = (
                    connection.execute(
                        select(m.documents).where(m.documents.c.material_key == doc["material_id"])
                    )
                    .mappings()
                    .first()
                )
            metadata = DocumentMetadata(
                source_kind=doc["source_kind"],
                origin_url=doc.get("origin_url"),
                retrieved_at=doc.get("retrieved_at"),
                effective_from=doc.get("effective_from"),
                effective_to=doc.get("effective_to"),
                raw_sources=doc.get("raw_sources", []),
                notes=doc.get("effective_note", ""),
                separate_project_annotations=doc["source_kind"] == "official_reference",
            )
            receipt = documents.import_document(
                DocumentImport(
                    title=doc["title"],
                    content=(root / doc["path"]).read_text(encoding="utf-8"),
                    metadata=metadata,
                    expected_version=current["version"] if current else 0,
                ),
                material_key=doc["material_id"],
            )
            bindings[doc["material_id"]] = receipt["revision_id"]
        # CLI 不启动第二个后台 worker；仅处理自己产生的文档任务。
        worker = KnowledgeWorker(store, KnowledgeProvider(settings))
        for revision_id in bindings.values():
            documents.index_revision(revision_id)
            with store.write() as connection:
                job = (
                    connection.execute(
                        select(m.jobs).where(m.jobs.c.idempotency_key == revision_id)
                    )
                    .mappings()
                    .one()
                )
                connection.execute(
                    m.jobs.update()
                    .where(m.jobs.c.id == job["id"], m.jobs.c.state != "running")
                    .values(state="succeeded", stage="ready", result={"index_mode": "text"})
                )
        metric_ids = []
        for definition in metric_package["definitions"]:
            with engine.connect() as connection:
                current = (
                    connection.execute(
                        select(m.metrics).where(m.metrics.c.key == definition["key"])
                    )
                    .mappings()
                    .first()
                )
            # 不覆盖使用者已经编辑/审核的指标；重复种子导入只补缺失项。
            if current:
                metric_ids.append(current["current_revision_id"])
                continue
            definition = {
                **definition,
                "source_revision_ids": [
                    bindings[key]
                    for key in metric_package["pending_source_bindings"][definition["key"]]
                ],
                "review_state": "draft",
                "version": 1,
            }
            metric_ids.append(MetricService(store).save(MetricWrite(definition=definition))["id"])
        embedding = (
            Retriever(store, worker.provider).index_embeddings(list(bindings.values()))
            if embed
            else None
        )
        return {
            "documents": bindings,
            "metric_draft_ids": metric_ids,
            "knowledge_snapshot": store.snapshot(),
            "embedding": embedding,
            "metrics_auto_approved": False,
        }
    finally:
        engine.dispose()


def main():
    """
    作用：提供 Windows PowerShell 兼容的本地导入命令
    输入：命令行参数
    输出：ASCII JSON 回执
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("knowledge_seed/manifest.json"))
    parser.add_argument("--embed", action="store_true")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                import_seed(Settings(), args.manifest, args.embed),
                ensure_ascii=True,
                allow_nan=False,
            )
        )
    except (DataError, ValueError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "SEED_IMPORT_FAILED")}, ensure_ascii=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
