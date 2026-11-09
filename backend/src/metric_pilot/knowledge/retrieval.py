"""固定知识快照内的 FTS5、向量余弦与 RRF 检索。"""

import math
import struct
from collections import defaultdict

from sqlalchemy import bindparam, select, text

from metric_pilot.data.common import DataError
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.documents import expand_query, query_terms
from metric_pilot.knowledge.relevance import focused_excerpt, is_substantive, relevance_bonus


class Retriever:
    """
    作用：持久化模型隔离向量并融合候选
    输入：知识仓储与模型入口
    输出：检索服务
    """

    def __init__(self, store, provider):
        """
        作用：组合服务依赖
        输入：仓储、模型适配器
        输出：无
        """
        self.store, self.provider = store, provider

    def index_embeddings(self, revision_ids):
        """
        作用：为固定原文补齐当前模型向量，已有匹配哈希不重复请求
        输入：文档修订 ID 列表
        输出：索引数量与向量维度
        """
        key = self.provider.model_key
        with self.store.engine.connect() as connection:
            pieces = [
                dict(r)
                for r in connection.execute(
                    select(m.chunks).where(m.chunks.c.revision_id.in_(revision_ids))
                ).mappings()
            ]
            existing = {
                r.chunk_id: r.text_hash
                for r in connection.execute(
                    select(m.embeddings).where(m.embeddings.c.model_key == key)
                ).mappings()
            }
            dimension = connection.execute(
                select(m.embeddings.c.dimension).where(m.embeddings.c.model_key == key).limit(1)
            ).scalar()
        pending = [piece for piece in pieces if existing.get(piece["id"]) != piece["text_hash"]]
        if not pieces or set(revision_ids) - {piece["revision_id"] for piece in pieces}:
            raise DataError("DOCUMENT_NOT_INDEXED")
        for start in range(0, len(pending), 32):
            batch = pending[start : start + 32]
            vectors = self.provider.embed([piece["text"] for piece in batch])
            if dimension is not None and len(vectors[0]) != dimension:
                raise DataError("EMBEDDING_DIMENSION_CONFLICT")
            dimension = len(vectors[0])
            with self.store.write() as connection:
                for piece, vector in zip(batch, vectors, strict=True):
                    connection.execute(
                        m.embeddings.delete().where(
                            m.embeddings.c.chunk_id == piece["id"], m.embeddings.c.model_key == key
                        )
                    )
                    connection.execute(
                        m.embeddings.insert().values(
                            chunk_id=piece["id"],
                            model_key=key,
                            text_hash=piece["text_hash"],
                            dimension=dimension,
                            vector=struct.pack(f"<{dimension}d", *vector),
                        )
                    )
        return {"indexed_chunks": len(pieces), "dimension": dimension, "model_key": key}

    def search(self, request):
        """
        作用：在服务端固定快照内按适用期检索，显式标记实际模式
        输入：SearchRequest
        输出：融合命中与限制
        """
        warnings = []
        with self.store.engine.connect() as connection:
            snapshot = self.store.get(connection, m.snapshots, request.snapshot_id)["payload"]
            allowed = []
            for ident in snapshot["document_revision_ids"]:
                revision = self.store.get(connection, m.revisions, ident)
                metadata = revision["metadata"]
                if request.as_of:
                    day = request.as_of.isoformat()
                    end = request.end_exclusive.isoformat() if request.end_exclusive else None
                    if metadata.get("effective_from") and (
                        metadata["effective_from"] >= end
                        if end
                        else day < metadata["effective_from"]
                    ):
                        continue
                    if metadata.get("effective_to") and day >= metadata["effective_to"]:
                        continue
                    if end and (
                        (metadata.get("effective_from") and metadata["effective_from"] > day)
                        or (metadata.get("effective_to") and metadata["effective_to"] < end)
                    ):
                        warnings.append("部分命中资料只覆盖所选范围的一部分，不能当作全范围规则。")
                if not metadata.get("effective_from"):
                    warnings.append("部分资料未声明历史生效期；命中不代表已验证其适用于所选日期。")
                allowed.append(ident)
            if not allowed:
                return {"items": [], "mode": request.mode, "warnings": ["此快照内没有适用资料。"]}
            pieces = {
                r.id: dict(r)
                for r in connection.execute(
                    select(m.chunks).where(m.chunks.c.revision_id.in_(allowed))
                ).mappings()
            }
            terms = query_terms(request.query)
            lexical = []
            wiki_hits = []
            if terms:
                statement = text(
                    "SELECT f.chunk_id FROM knowledge_fts f "
                    "JOIN knowledge_chunks c ON c.id=f.chunk_id "
                    "WHERE knowledge_fts MATCH :query AND c.revision_id IN :ids "
                    "ORDER BY bm25(knowledge_fts), f.chunk_id LIMIT 50"
                ).bindparams(bindparam("ids", expanding=True))
                lexical = list(
                    connection.execute(statement, {"query": terms, "ids": allowed}).scalars()
                )
                if snapshot["wiki_revision_ids"]:
                    statement = text(
                        "SELECT chunk_id FROM knowledge_fts WHERE knowledge_fts MATCH :query "
                        "AND chunk_id IN :ids ORDER BY bm25(knowledge_fts), chunk_id LIMIT 8"
                    ).bindparams(bindparam("ids", expanding=True))
                    for ident in connection.execute(
                        statement,
                        {
                            "query": terms,
                            "ids": snapshot["wiki_revision_ids"],
                        },
                    ).scalars():
                        revision = self.store.get(connection, m.wiki_revisions, ident)
                        if set(revision["payload"]["input_revision_ids"]) <= set(allowed):
                            wiki_hits.append(revision)
            vectors = (
                list(
                    connection.execute(
                        select(m.embeddings).where(
                            m.embeddings.c.model_key == self.provider.model_key,
                            m.embeddings.c.chunk_id.in_(pieces),
                        )
                    ).mappings()
                )
                if request.mode == "hybrid"
                else []
            )
        ranked = [lexical]
        if request.mode == "hybrid":
            if not self.provider.settings.integration_configured("embedding"):
                raise DataError("MODEL_NOT_CONFIGURED", {"integration": "embedding"})
            if not vectors:
                raise DataError("EMBEDDING_INDEX_REQUIRED")
            vector = self.provider.embed([expand_query(request.query)])[0]
            norm = math.sqrt(sum(x * x for x in vector))
            similarities = []
            for row in vectors:
                if row["dimension"] != len(vector):
                    raise DataError("EMBEDDING_DIMENSION_CONFLICT")
                if row["text_hash"] != pieces[row["chunk_id"]]["text_hash"]:
                    raise DataError("EMBEDDING_HASH_MISMATCH")
                stored = struct.unpack(f"<{row['dimension']}d", row["vector"])
                score = sum(a * b for a, b in zip(vector, stored, strict=True)) / (
                    norm * math.sqrt(sum(x * x for x in stored))
                )
                similarities.append((score, row["chunk_id"]))
            ranked.append(
                [
                    ident
                    for _, ident in sorted(similarities, key=lambda pair: (-pair[0], pair[1]))[:50]
                ]
            )
            if len(vectors) < len(pieces):
                warnings.append("部分片段尚无当前模型向量；这些片段仅参与文本检索。")
        # 两路分值不直接相加，按名次使用 RRF，结果只从固定原文读取。
        scores = defaultdict(float)
        for ranking in ranked:
            for rank, ident in enumerate(ranking, 1):
                scores[ident] += 1 / (60 + rank)
        candidates = []
        for ident, score in scores.items():
            piece = pieces[ident]
            if is_substantive(piece, request.query):
                bonus, reason = relevance_bonus(piece, request.query)
                candidates.append(
                    {
                        "chunk": piece,
                        "score": score + bonus,
                        "excerpt": focused_excerpt(piece["text"], request.query),
                        "match_reason": reason,
                    }
                )
        hits, seen = [], set()
        for hit in sorted(candidates, key=lambda hit: (-hit["score"], hit["chunk"]["id"])):
            group = (hit["chunk"]["revision_id"], hit["chunk"]["locator"]["section"])
            if group not in seen:
                hits.append(hit)
                seen.add(group)
            if len(hits) == 12:
                break
        return {
            "items": hits,
            "mode": request.mode,
            "warnings": sorted(set(warnings)),
            "wiki_items": wiki_hits,
        }
