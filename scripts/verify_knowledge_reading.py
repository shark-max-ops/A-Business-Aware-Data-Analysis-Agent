"""对用户反馈的真实查询做检索验收，不生成或审核规则。"""

import argparse
import json

from metric_pilot.data.common import utc_now
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge.contracts import SearchRequest
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.settings import Settings


def verify(live=False):
    """
    作用：记录真实材料上两种说法的直接依据排名和逐字摘录
    输入：是否显式请求真实 query embedding
    输出：验收是否通过
    """
    settings = Settings()
    engine = create_metadata_engine(settings)
    report = {"created_at": utc_now(), "live_embedding_requested": live, "cases": []}
    try:
        store = KnowledgeStore(engine)
        snapshot = store.snapshot()
        report["snapshot_id"] = snapshot["id"]
        retriever = Retriever(store, KnowledgeProvider(settings))
        for mode in ["text", "hybrid"] if live else ["text"]:
            for query in ("提前到达为什么是负数", "航班早到了，为什么还有负的延误分钟？"):
                result = retriever.search(
                    SearchRequest(
                        query=query, mode=mode, snapshot_id=snapshot["id"], as_of="2026-01-15"
                    )
                )
                first = result["items"][0] if result["items"] else None
                passed = bool(
                    first
                    and first["chunk"]["source_kind"] == "official_reference"
                    and first["chunk"]["locator"]["section"] == "ArrDelay"
                    and "Early arrivals show negative numbers" in first["excerpt"]
                )
                report["cases"].append(
                    {
                        "query": query,
                        "mode": mode,
                        "passed": passed,
                        "first_results": [
                            {
                                "section": h["chunk"]["locator"]["section"],
                                "source_kind": h["chunk"]["source_kind"],
                                "chunk_id": h["chunk"]["id"],
                                "excerpt": h["excerpt"],
                            }
                            for h in result["items"][:3]
                        ],
                        "excerpts_are_verbatim": all(
                            h["excerpt"] in h["chunk"]["text"] for h in result["items"]
                        ),
                    }
                )
        report["passed"] = all(
            case["passed"] and case["excerpts_are_verbatim"] for case in report["cases"]
        )
        target = settings.storage_dir / "validation" / "stage2-reading-feedback.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))
        return report["passed"]
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-embedding", action="store_true")
    raise SystemExit(0 if verify(parser.parse_args().live_embedding) else 1)
