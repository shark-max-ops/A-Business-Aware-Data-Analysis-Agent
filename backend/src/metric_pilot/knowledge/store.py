"""知识存储事务和引用验证；所有 ID 由服务端分配。"""

import hashlib
from contextlib import contextmanager

from sqlalchemy import select

from metric_pilot.data.common import DataError, new_id, utc_now
from metric_pilot.knowledge import models as m


def text_hash(text: str) -> str:
    """
    作用：计算原文 UTF-8 摘要
    输入：原文
    输出：SHA-256
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KnowledgeStore:
    """
    作用：组合元数据库事务与不可变知识引用
    输入：SQLAlchemy 引擎
    输出：知识存储服务
    """

    def __init__(self, engine):
        """
        作用：固定数据库依赖
        输入：engine - 元数据库引擎
        输出：无
        """
        self.engine = engine

    @contextmanager
    def write(self):
        """
        作用：串行化写入，避免检查版本后并发覆盖
        输入：无
        输出：事务连接
        """
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection

    def get(self, connection, table, identifier, column="id"):
        """
        作用：按固定表和键读取记录
        输入：连接、内部表、键和值
        输出：记录或安全错误
        """
        row = (
            connection.execute(select(table).where(table.c[column] == identifier))
            .mappings()
            .first()
        )
        if row is None:
            raise DataError("KNOWLEDGE_NOT_FOUND")
        return dict(row)

    def citations(self, connection, citations, revision_ids):
        """
        作用：拒绝不存在、越界或伪造摘录的引用
        输入：连接、引用和本次固定输入文档
        输出：无
        """
        for revision_id in revision_ids:
            self.get(connection, m.revisions, revision_id)
        for citation in citations:
            chunk = self.get(connection, m.chunks, citation["chunk_id"])
            if (
                chunk["revision_id"] not in revision_ids
                or citation["text_hash"] != chunk["text_hash"]
                or citation["excerpt"] not in chunk["text"]
            ):
                raise DataError("INVALID_CITATION")

    def snapshot(self):
        """
        作用：冻结当前文档、发布 Wiki 和审核指标的修订清单
        输入：无
        输出：服务端知识快照
        """
        with self.write() as connection:
            doc_ids = list(connection.execute(select(m.documents.c.current_revision_id)).scalars())
            wiki_ids = list(
                connection.execute(
                    select(m.pages.c.published_revision_id).where(
                        m.pages.c.published_revision_id.is_not(None)
                    )
                ).scalars()
            )
            metric_ids = list(
                connection.execute(
                    select(m.metrics.c.approved_revision_id).where(
                        m.metrics.c.approved_revision_id.is_not(None)
                    )
                ).scalars()
            )
            # 正式规则和 Wiki 可能引用旧文档；这些原文也必须可见。
            for ident in wiki_ids:
                doc_ids.extend(
                    self.get(connection, m.wiki_revisions, ident)["payload"]["input_revision_ids"]
                )
            for ident in metric_ids:
                doc_ids.extend(
                    self.get(connection, m.metric_revisions, ident)["definition"][
                        "source_revision_ids"
                    ]
                )
            payload = {
                "document_revision_ids": sorted(set(doc_ids)),
                "wiki_revision_ids": sorted(wiki_ids),
                "metric_revision_ids": sorted(metric_ids),
            }
            ident = new_id("ks")
            connection.execute(
                m.snapshots.insert().values(id=ident, payload=payload, created_at=utc_now())
            )
            return {"id": ident, **payload}

    def enqueue(self, connection, kind, payload, key=None):
        """
        作用：登记持久化且可幂等复用的知识任务
        输入：事务、任务类型、固定输入、幂等键
        输出：任务标识
        """
        if key:
            existing = connection.execute(
                select(m.jobs.c.id).where(m.jobs.c.idempotency_key == key)
            ).scalar()
            if existing:
                return existing
        ident = new_id("job")
        connection.execute(
            m.jobs.insert().values(
                id=ident,
                kind=kind,
                state="queued",
                stage="queued",
                payload=payload,
                result={},
                error_code=None,
                idempotency_key=key,
                created_at=utc_now(),
            )
        )
        return ident
