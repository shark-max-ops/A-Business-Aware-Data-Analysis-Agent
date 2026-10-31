"""根据已保存引用构建阅读关系图，不从模型文本猜测知识关系。"""

from sqlalchemy import select

from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.store import text_hash


class KnowledgeGraph:
    """
    作用：把真实文档、引用主题、Wiki 和指标来源连成可导航图
    输入：知识仓储
    输出：关系图服务
    """

    def __init__(self, store):
        """
        作用：固定读取依赖
        输入：知识仓储
        输出：无
        """
        self.store = store

    def build(self, page_id):
        """
        作用：读取选中 Wiki 固定修订的引用和共享来源的指标
        输入：页面 ID
        输出：真实来源节点、关系和范围说明
        """
        nodes, edges = {}, {}
        with self.store.engine.connect() as connection:
            page = self.store.get(connection, m.pages, page_id)
            wiki = self.store.get(connection, m.wiki_revisions, page["current_revision_id"])
            payload = wiki["payload"]
            nodes[wiki["id"]] = {
                "id": wiki["id"],
                "kind": "wiki",
                "label": payload["title"],
                "status": payload["state"],
                "source_kind": "generated_wiki",
            }

            def document_node(revision_id):
                """
                作用：解析真实来源修订并生成文档节点
                输入：原文修订 ID
                输出：文档图节点 ID
                """
                revision = self.store.get(connection, m.revisions, revision_id)
                document = self.store.get(connection, m.documents, revision["document_id"])
                nodes[revision_id] = {
                    "id": revision_id,
                    "kind": "document",
                    "label": document["title"],
                    "status": f"v{revision['version']}",
                    "source_kind": revision["metadata"]["source_kind"],
                    "document_id": document["id"],
                    "revision_id": revision_id,
                }
                return revision_id

            source_ids = set(payload["input_revision_ids"])
            for citation in payload["citations"]:
                chunk = self.store.get(connection, m.chunks, citation["chunk_id"])
                source = document_node(chunk["revision_id"])
                section = chunk["locator"].get("section", "正文")
                concept = (
                    "topic_" + text_hash(source + "|" + section + "|" + chunk["source_kind"])[:24]
                )
                nodes.setdefault(
                    concept,
                    {
                        "id": concept,
                        "kind": "concept",
                        "label": section,
                        "source_kind": chunk["source_kind"],
                        "chunk_id": chunk["id"],
                        "revision_id": source,
                        "document_id": nodes[source]["document_id"],
                    },
                )
                edges[(source, concept)] = {
                    "source": source,
                    "target": concept,
                    "label": "包含引用主题",
                }
                edges[(concept, wiki["id"])] = {
                    "source": concept,
                    "target": wiki["id"],
                    "label": "引用",
                }
            for row in connection.execute(select(m.metrics).order_by(m.metrics.c.key)).mappings():
                revision = self.store.get(
                    connection, m.metric_revisions, row["current_revision_id"]
                )
                definition = revision["definition"]
                if not source_ids.intersection(definition["source_revision_ids"]):
                    continue
                nodes[revision["id"]] = {
                    "id": revision["id"],
                    "kind": "metric",
                    "label": definition["name"],
                    "status": definition["review_state"],
                    "source_kind": "project_policy",
                    "metric_key": definition["key"],
                }
                for source in definition["source_revision_ids"]:
                    document_node(source)
                    edges[(source, revision["id"])] = {
                        "source": source,
                        "target": revision["id"],
                        "label": "口径依据",
                    }
        return {
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "page_id": page_id,
            "wiki_revision_id": wiki["id"],
            "scope": "当前 Wiki 修订及其真实引用；指标含待审核草稿。",
            "note": "连线表示引用或口径来源，不表示因果关系，也不代表草稿已经审核。",
        }
