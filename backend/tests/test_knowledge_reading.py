"""用户反馈的改写检索、片段长度和真实关系图回归。"""

import pytest
import test_knowledge as knowledge_support
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.contracts import DocumentMetadata, MetricWrite, SearchRequest
from metric_pilot.knowledge.documents import DocumentService
from metric_pilot.knowledge.graph import KnowledgeGraph
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.relevance import focused_excerpt
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.wiki import WikiService
from sqlalchemy import select
from test_knowledge import add_document, metric_definition, wiki_payload

knowledge = knowledge_support.knowledge


@pytest.mark.parametrize("query", ["提前到达为什么是负数", "航班早到了，为什么还有负的延误分钟？"])
@pytest.mark.parametrize("mode", ["text", "hybrid"])
def test_paraphrased_question_prefers_direct_official_definition(knowledge, query, mode):
    """
    作用：即使语义候选偏向中文例子，也优先展示官方字段解释
    输入：隔离库、两种问法和检索模式
    输出：无
    """
    store, provider = knowledge
    official = add_document(
        store,
        "# ArrDelay\n\nDifference in minutes between scheduled and actual arrival time. "
        "Early arrivals show negative numbers.",
        metadata=DocumentMetadata(
            source_kind="official_reference", origin_url="https://example.org/fields"
        ),
    )
    add_document(
        store,
        "# P05：两个容易混淆的均值\n\n例如两班航班分别提前 10 分钟、晚到 20 分钟："
        "平均到达偏差为 5 分钟，平均非负延误为 10 分钟。这是教学假设。",
    )
    add_document(
        store,
        "# P04：延误率\n\neligible_arrivals = E 的记录数。"
        "arrival_delay_rate = arrival_delayed_15=1 的记录数 / E 的记录数。",
    )
    fixed = store.snapshot()
    retriever = Retriever(store, provider)
    if mode == "hybrid":
        retriever.index_embeddings(fixed["document_revision_ids"])
    result = retriever.search(SearchRequest(query=query, snapshot_id=fixed["id"], mode=mode))
    first = result["items"][0]
    assert first["chunk"]["revision_id"] == official["revision_id"]
    assert first["chunk"]["source_kind"] == "official_reference"
    assert "Early arrivals show negative numbers" in first["excerpt"]
    assert first["match_reason"] == "直接匹配字段释义"
    assert all(not hit["chunk"]["text"].strip().startswith("#") for hit in result["items"])
    assert len(
        {(h["chunk"]["revision_id"], h["chunk"]["locator"]["section"]) for h in result["items"]}
    ) == len(result["items"])


def test_excerpt_remains_source_text_and_table_rows_are_not_invented():
    """
    作用：摘要是原文连续子串，短表格保持完整
    输入：无
    输出：无
    """
    text = "这是范围说明。" * 100 + "提前到达的 ArrDelay 可以为负数。" + "其他说明。" * 100
    excerpt = focused_excerpt(text, "提前到达为什么是负数")
    assert excerpt in text and len(excerpt) <= 320
    table = "| 指标 | 定义 |\n| --- | --- |\n| ArrDelay | 保留负数 |"
    assert focused_excerpt(table, "到达偏差", limit=20) == table


def test_context_reindex_preserves_original_chunks(knowledge):
    """
    作用：补充标题索引不修改既有片段 ID、原文或旧引用
    输入：隔离知识库
    输出：无
    """
    store, _ = knowledge
    receipt = add_document(store, "# ArrDelay\n\nEarly arrivals show negative numbers.")
    with store.engine.connect() as c:
        before = [dict(r) for r in c.execute(select(m.chunks)).mappings()]
    DocumentService(store).index_revision(receipt["revision_id"])
    with store.engine.connect() as c:
        after = [dict(r) for r in c.execute(select(m.chunks)).mappings()]
        body = c.exec_driver_sql(
            "SELECT body FROM knowledge_fts WHERE chunk_id=?", (before[-1]["id"],)
        ).scalar_one()
    assert before == after and "ArrDelay" in body


def test_graph_edges_only_follow_actual_citations_and_metric_sources(knowledge):
    """
    作用：图关系来自实际输入，排除无关指标并保留草稿状态
    输入：隔离知识库
    输出：无
    """
    store, provider = knowledge
    receipt = add_document(store, "# ArrDelay\n\nEarly arrivals show negative numbers.")
    wiki = WikiService(store, provider).save("arrival", wiki_payload(store, receipt), 0)
    metric = MetricService(store).save(
        MetricWrite(definition=metric_definition(receipt["revision_id"]))
    )
    other = add_document(store, "Other unrelated source")
    unrelated = metric_definition(other["revision_id"])
    unrelated["key"] = "other_metric"
    MetricService(store).save(MetricWrite(definition=unrelated))
    graph = KnowledgeGraph(store).build(wiki["page_id"])
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes[wiki["id"]]["status"] == "draft"
    assert nodes[metric["id"]]["status"] == "draft"
    assert not any(n.get("metric_key") == "other_metric" for n in nodes.values())
    for edge in graph["edges"]:
        assert edge["source"] in nodes and edge["target"] in nodes
        assert edge["label"] in {"引用", "口径依据", "包含引用主题"}
    assert any(
        e["source"] == receipt["revision_id"] and e["target"] == metric["id"]
        for e in graph["edges"]
    )
