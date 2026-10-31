"""Wiki 草稿、来源约束、乐观锁、发布与非破坏回滚。"""

import re

from sqlalchemy import select

from metric_pilot.data.common import DataError, canonical_bytes, new_id, utc_now
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.contracts import WikiCandidate
from metric_pilot.knowledge.documents import index_text
from metric_pilot.knowledge.store import text_hash


class WikiService:
    """
    作用：组合 Wiki 来源校验与版本操作
    输入：仓储与模型入口
    输出：Wiki 服务
    """

    def __init__(self, store, provider):
        """
        作用：固定依赖
        输入：知识仓储、模型适配器
        输出：无
        """
        self.store, self.provider = store, provider

    def validate(self, connection, payload):
        """
        作用：验证正文引用确实来自输入，不自动证明语义正确
        输入：连接与 Wiki 候选
        输出：无
        """
        self.store.citations(connection, payload["citations"], payload["input_revision_ids"])
        ids = {citation["chunk_id"] for citation in payload["citations"]}
        content = payload["content"]
        used = set(re.findall(r"\[(chunk_[A-Za-z0-9_]+)\]", content))
        if not used or not used <= ids or not ids <= used:
            raise DataError("WIKI_CITATIONS_REQUIRED")
        for paragraph in re.split(r"\n\s*\n", content):
            body = "\n".join(line for line in paragraph.splitlines() if not line.startswith("#"))
            if body.strip() and not re.search(r"\[(chunk_[A-Za-z0-9_]+)\]", body):
                raise DataError("UNCITED_WIKI_PARAGRAPH")

    def save(self, slug, payload, expected_version, edit_source="manual"):
        """
        作用：追加草稿或发布修订，绝不覆盖已有正文
        输入：slug、内容、预期版本和编辑来源
        输出：新修订记录
        """
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug):
            raise DataError("INVALID_WIKI_SLUG")
        with self.store.write() as connection:
            page = (
                connection.execute(select(m.pages).where(m.pages.c.slug == slug)).mappings().first()
            )
            version = page["version"] if page else 0
            if expected_version != version:
                raise DataError("VERSION_CONFLICT", {"current_version": version})
            self.validate(connection, payload)
            ident, page_id = new_id("wikirev"), page["id"] if page else new_id("wiki")
            payload = {
                **payload,
                "edit_source": edit_source,
                "source_kind": "generated_wiki",
                "state": payload.get("state", "draft"),
                "content_hash": text_hash(payload["content"]),
            }
            if not page:
                count = connection.execute(select(m.pages.c.id)).all()
                if len(count) >= 40:
                    raise DataError("WIKI_PAGE_LIMIT")
                connection.execute(
                    m.pages.insert().values(
                        id=page_id,
                        slug=slug,
                        version=version + 1,
                        current_revision_id=ident,
                        published_revision_id=None,
                    )
                )
            else:
                connection.execute(
                    m.pages.update()
                    .where(m.pages.c.id == page_id)
                    .values(version=version + 1, current_revision_id=ident)
                )
            connection.execute(
                m.wiki_revisions.insert().values(
                    id=ident,
                    page_id=page_id,
                    version=version + 1,
                    payload=payload,
                    created_at=utc_now(),
                )
            )
            if payload["state"] == "published":
                connection.execute(
                    m.pages.update()
                    .where(m.pages.c.id == page_id)
                    .values(published_revision_id=ident)
                )
                connection.exec_driver_sql(
                    "INSERT INTO knowledge_fts(chunk_id, body) VALUES (?, ?)",
                    (ident, index_text(payload["title"] + "\n" + payload["content"])),
                )
            return {
                "id": ident,
                "page_id": page_id,
                "slug": slug,
                "version": version + 1,
                "payload": payload,
            }

    def action(self, page_id, request, publish):
        """
        作用：发布当前草稿或从历史创建新草稿
        输入：页面、目标修订、乐观锁和操作类型
        输出：新修订
        """
        with self.store.engine.connect() as connection:
            page = self.store.get(connection, m.pages, page_id)
            revision = self.store.get(connection, m.wiki_revisions, request.revision_id)
            if revision["page_id"] != page_id:
                raise DataError("REVISION_SCOPE_MISMATCH")
            if publish and (
                page["current_revision_id"] != request.revision_id
                or revision["payload"]["state"] != "draft"
            ):
                raise DataError("WIKI_CURRENT_DRAFT_REQUIRED")
            payload = {
                **revision["payload"],
                "state": "published" if publish else "draft",
                "parent_revision_id": request.revision_id,
            }
        return self.save(
            page["slug"], payload, request.expected_version, "publish" if publish else "rollback"
        )

    def queue_generation(self, request):
        """
        作用：冻结输入及页面基线，重复请求复用同一个任务
        输入：生成请求
        输出：任务 ID
        """
        if not self.provider.settings.integration_configured("llm"):
            raise DataError("MODEL_NOT_CONFIGURED", {"integration": "llm"})
        payload = request.model_dump(mode="json")
        payload["input_revision_ids"] = sorted(set(payload["input_revision_ids"]))
        with self.store.write() as connection:
            page = (
                connection.execute(select(m.pages).where(m.pages.c.slug == request.slug))
                .mappings()
                .first()
            )
            current = page["version"] if page else 0
            if current != request.expected_version:
                raise DataError("VERSION_CONFLICT", {"current_version": current})
            for ident in request.input_revision_ids:
                self.store.get(connection, m.revisions, ident)
                if not connection.execute(
                    select(m.chunks.c.id).where(m.chunks.c.revision_id == ident).limit(1)
                ).first():
                    raise DataError("DOCUMENT_NOT_INDEXED")
            key = text_hash(canonical_bytes(payload).decode())
            return self.store.enqueue(connection, "wiki", payload, key)

    def generate(self, payload):
        """
        作用：提取每文档候选、确定性去重，再合并有来源草稿
        输入：任务固定输入
        输出：保存结果与真实模型用量
        """
        with self.store.engine.connect() as connection:
            pieces = [
                dict(row)
                for row in connection.execute(
                    select(m.chunks)
                    .where(m.chunks.c.revision_id.in_(payload["input_revision_ids"]))
                    .order_by(m.chunks.c.revision_id, m.chunks.c.id)
                ).mappings()
            ]
        # 按原文坐标恢复段落顺序，不能将随机片段 ID 当作阅读顺序。
        pieces.sort(key=lambda piece: (piece["revision_id"], piece["locator"]["char_start"]))
        # 本次上下文最多 48k 字符，超限明确拒绝，避免悄悄丢掉输入。
        if sum(len(piece["text"]) for piece in pieces) > 48000:
            raise DataError("WIKI_INPUT_TOO_LARGE", {"hint": "请减少本次输入文档。"})
        # 同文同段摘要只输入一次；模型只返回解释，不能修改指标或发布权限。
        unique = {piece["text_hash"]: piece for piece in pieces}
        feedback, attempts = None, []
        for attempt in range(2):
            try:
                candidate, usage = self.provider.generate(list(unique.values()), feedback)
                attempts.append(usage)
                validated = WikiCandidate.model_validate(candidate).model_dump(mode="json")
                with self.store.engine.connect() as connection:
                    self.validate(
                        connection,
                        {**validated, "input_revision_ids": payload["input_revision_ids"]},
                    )
                break
            except DataError as exc:
                if attempt or exc.code not in {
                    "INVALID_GENERATION_RESPONSE",
                    "WIKI_CITATIONS_REQUIRED",
                    "UNCITED_WIKI_PARAGRAPH",
                    "INVALID_CITATION",
                    "KNOWLEDGE_NOT_FOUND",
                }:
                    raise
                feedback = exc.code
            except ValueError:
                feedback = "INVALID_GENERATION_RESPONSE"
                if attempt:
                    raise DataError(feedback) from None
        result = self.save(
            payload["slug"],
            {**validated, "input_revision_ids": payload["input_revision_ids"]},
            payload["expected_version"],
            "model",
        )
        return {
            "revision_id": result["id"],
            "page_id": result["page_id"],
            "version": result["version"],
            "usage": {"attempts": attempts},
            "validation": "references_validated_requires_human_review",
        }
