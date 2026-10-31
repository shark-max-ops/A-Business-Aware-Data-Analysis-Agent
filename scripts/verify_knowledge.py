"""阶段 2 本地真实材料验收；远程模型仅在显式参数下调用。"""

import argparse
import json
import time

from metric_pilot.data.common import DataError, utc_now
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.contracts import GenerateWiki, SearchRequest
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.knowledge.wiki import WikiService
from metric_pilot.settings import Settings
from sqlalchemy import select


def verify(live_models=False):
    """
    作用：验收实际导入材料、中文检索、向量和 Wiki 草稿
    输入：是否显式请求真实模型
    输出：保存于忽略目录的验收结果
    """
    config = Settings()
    engine = create_metadata_engine(config)
    report = {
        "kind": "stage2_real_material_acceptance",
        "created_at": utc_now(),
        "live_models_requested": live_models,
        "checks": {},
        "errors": [],
    }
    try:
        store = KnowledgeStore(engine)
        provider = KnowledgeProvider(config)
        retriever = Retriever(store, provider)
        with engine.connect() as connection:
            docs = [dict(row) for row in connection.execute(select(m.documents)).mappings()]
            report["documents"] = {
                d["material_key"]: d["current_revision_id"] for d in docs if d["material_key"]
            }
            report["chunks"] = len(
                connection.execute(
                    select(m.chunks.c.id).where(
                        m.chunks.c.revision_id.in_([d["current_revision_id"] for d in docs])
                    )
                ).all()
            )
            report["metrics"] = [
                dict(row) for row in connection.execute(select(m.metrics)).mappings()
            ]
        report["checks"]["seven_seed_documents"] = len(report["documents"]) == 7
        report["checks"]["eight_metric_drafts_not_auto_approved"] = len(
            report["metrics"]
        ) == 8 and all(row["approved_revision_id"] is None for row in report["metrics"])
        fixed = store.snapshot()
        report["knowledge_snapshot_id"] = fixed["id"]
        requests = [
            ("提前到达为什么是负数", "Early arrivals"),
            ("取消是否进入分母", "cancelled"),
            ("延误十五分钟", "15"),
        ]
        report["text_search"] = []
        for query, needle in requests:
            result = retriever.search(
                SearchRequest(query=query, snapshot_id=fixed["id"], as_of="2026-01-15")
            )
            passed = any(needle in item["chunk"]["text"] for item in result["items"])
            report["text_search"].append(
                {
                    "query": query,
                    "passed": passed,
                    "chunk_ids": [r["chunk"]["id"] for r in result["items"]],
                }
            )
        report["checks"]["chinese_text_search"] = all(
            item["passed"] for item in report["text_search"]
        )
        if live_models:
            print(
                json.dumps({"step": "building_real_embedding_index", "chunks": report["chunks"]}),
                flush=True,
            )
            try:
                start = time.monotonic()
                report["embedding"] = retriever.index_embeddings(fixed["document_revision_ids"])
                result = retriever.search(
                    SearchRequest(
                        query="提前到达为什么是负数",
                        snapshot_id=fixed["id"],
                        as_of="2026-01-15",
                        mode="hybrid",
                    )
                )
                report["hybrid_search"] = {
                    "mode": result["mode"],
                    "hits": len(result["items"]),
                    "chunk_ids": [r["chunk"]["id"] for r in result["items"]],
                }
                report["checks"]["real_embedding_and_hybrid"] = (
                    bool(result["items"]) and result["mode"] == "hybrid"
                )
                report["embedding_duration_ms"] = round((time.monotonic() - start) * 1000)
            except DataError as exc:
                report["errors"].append(
                    {"step": "embedding", "code": exc.code, "details": exc.details}
                )
                report["checks"]["real_embedding_and_hybrid"] = False
            print(json.dumps({"step": "real_wiki_generation"}), flush=True)
            try:
                with engine.connect() as connection:
                    page = (
                        connection.execute(
                            select(m.pages).where(m.pages.c.slug == "bts-arrival-fields")
                        )
                        .mappings()
                        .first()
                    )
                request = GenerateWiki(
                    slug="bts-arrival-fields",
                    input_revision_ids=[report["documents"]["bts-selected-fields-v1"]],
                    expected_version=page["version"] if page else 0,
                )
                wiki = WikiService(store, provider)
                job = None
                with engine.connect() as connection:
                    for previous in connection.execute(
                        select(m.jobs).where(m.jobs.c.kind == "wiki", m.jobs.c.state == "succeeded")
                    ).mappings():
                        if (
                            page
                            and previous["result"].get("revision_id") == page["current_revision_id"]
                        ):
                            revision = store.get(
                                connection, m.wiki_revisions, page["current_revision_id"]
                            )
                            if (
                                revision["payload"]["input_revision_ids"]
                                == request.input_revision_ids
                            ):
                                wiki.validate(connection, revision["payload"])
                                job = dict(previous)
                                break
                reused = job is not None
                if job is None:
                    job_id = wiki.queue_generation(request)
                    # 正常 API 的 worker 执行实际任务，CLI 只轮询持久化状态。
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        with engine.connect() as connection:
                            job = store.get(connection, m.jobs, job_id)
                        if job["state"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.5)
                report["wiki_generation"] = {
                    "job_id": job["id"],
                    "reused_successful_model_draft": reused,
                    "state": job["state"],
                    "error_code": job["error_code"],
                    "result": job["result"],
                }
                report["checks"]["real_wiki_draft_with_valid_citations"] = (
                    job["state"] == "succeeded"
                )
                if job["state"] == "succeeded":
                    with engine.connect() as connection:
                        revision = store.get(
                            connection, m.wiki_revisions, job["result"]["revision_id"]
                        )
                        report["wiki_generation"]["state_is_draft"] = (
                            revision["payload"]["state"] == "draft"
                        )
                        report["wiki_generation"]["citations"] = len(
                            revision["payload"]["citations"]
                        )
                else:
                    report["errors"].append(
                        {"step": "wiki", "code": job["error_code"] or "JOB_DEADLINE"}
                    )
            except DataError as exc:
                report["errors"].append({"step": "wiki", "code": exc.code, "details": exc.details})
                report["checks"]["real_wiki_draft_with_valid_citations"] = False
        else:
            report["model_validation"] = "not_requested"
        report["all_requested_checks_passed"] = all(report["checks"].values())
        target = (
            config.storage_dir
            / "validation"
            / ("stage2-knowledge.json" if live_models else "stage2-knowledge-local.json")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "checks": report["checks"],
                    "errors": report["errors"],
                    "all_requested_checks_passed": report["all_requested_checks_passed"],
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        return report["all_requested_checks_passed"]
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-models", action="store_true")
    raise SystemExit(0 if verify(parser.parse_args().live_models) else 1)
