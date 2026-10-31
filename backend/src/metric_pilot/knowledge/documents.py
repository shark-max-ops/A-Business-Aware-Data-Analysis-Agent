"""文档修订、可定位分块与中英文索引文本。"""

import re

from sqlalchemy import select

from metric_pilot.data.common import DataError, new_id, utc_now
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.store import text_hash

ALIASES = {
    "提前到达": "ArrDelay arrival_delay early arrivals negative",
    "早到": "ArrDelay arrival_delay early arrivals negative",
    "提前抵达": "ArrDelay arrival_delay early arrivals negative",
    "负的延误": "ArrDelay arrival_delay early arrivals negative",
    "负数": "ArrDelay arrival_delay negative",
    "延误率": "ArrDel15 arrival_delay_rate eligible_arrivals",
    "非负延误": "ArrDelayMinutes arrival_delay_nonnegative",
    "取消": "Cancelled cancelled cancellation_rate",
    "备降": "Diverted diverted DivArrDelay",
    "分母": "denominator eligible_arrivals scheduled_flights",
    "天气": "WeatherDelay delay_weather weather",
    "原因": "CarrierDelay WeatherDelay NASDelay SecurityDelay LateAircraftDelay causes",
    "航空公司": "Reporting_Airline carrier_id airline",
    "机场": "OriginAirportID DestAirportID airport_id",
    "利润": "profit revenue cost",
}

FIELD_SYNONYMS = {
    "ArrDelay": (
        "arrival_delay",
        "提前到达",
        "早到",
        "提前抵达",
        "负数",
        "负值",
        "负的延误",
        "到达偏差",
    ),
    "ArrDelayMinutes": ("arrival_delay_nonnegative", "非负延误", "非负到达"),
    "ArrDel15": ("arrival_delayed_15", "十五分钟", "15分钟", "15 分钟"),
    "Cancelled": ("cancelled", "取消"),
    "Diverted": ("diverted", "备降"),
    "WeatherDelay": ("delay_weather", "天气"),
}


def expand_query(query):
    """
    作用：给文本和向量检索使用同一组中英术语，不改变用户问题
    输入：原问题
    输出：包含领域别名的查询文本
    """
    expansions = list(dict.fromkeys(value for key, value in ALIASES.items() if key in query))
    return query + ("\n" + " ".join(expansions) if expansions else "")


def focused_fields(query):
    """
    作用：识别字段级同义词，为解释段落排序提供上下文
    输入：中文或英文查询
    输出：实际匹配的字段名集合
    """
    found = set()
    lower = query.lower()
    for field, synonyms in FIELD_SYNONYMS.items():
        for term in (field, *synonyms):
            if re.search(r"[\u4e00-\u9fff]", term):
                matched = term in query
            else:
                matched = bool(
                    re.search(r"(?<![a-z0-9_])" + re.escape(term.lower()) + r"(?![a-z0-9_])", lower)
                )
            if matched:
                found.add(field)
                break
    return found


def index_text(text):
    """
    作用：为中文显式加入单字和相邻双字词，不假设 FTS5 自动分中文
    输入：原文
    输出：检索索引文本
    """
    pairs = []
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        pairs.extend(run)
        pairs.extend(run[i : i + 2] for i in range(len(run) - 1))
    return text + " " + " ".join(pairs)


def query_terms(query):
    """
    作用：扩展中英术语并转义 FTS 查询语法
    输入：用户搜索词
    输出：有限 OR 表达式
    """
    expanded = expand_query(query)
    tokens = re.findall(r"[a-zA-Z0-9_]+", expanded)
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        tokens.extend([run] if len(run) < 2 else [run[i : i + 2] for i in range(len(run) - 1)])
    return " OR ".join(f'"{term}"' for term in dict.fromkeys(tokens[:60]))


def split_document(content, metadata):
    """
    作用：按标题段落分块，保留精确字符与行坐标及段落权威
    输入：原文与元数据
    输出：片段列表
    """
    result, section, source_locator = [], "正文", ""
    project_section = False
    # 每块最多约 2400 字符；超长段落重叠 200 字符，坐标直接指向原文。
    for match in re.finditer(r"\S[\s\S]*?(?=\n[ \t]*\n|\Z)", content):
        paragraph = match.group().rstrip()
        if paragraph.startswith("#"):
            section = paragraph.lstrip("# ").splitlines()[0]
            source_locator = ""
            project_section = any(
                word in section for word in ("项目", "阅读提示", "实际检查", "使用限制", "编码说明")
            )
        if paragraph.startswith("原文定位："):
            source_locator = paragraph
        kind = metadata["source_kind"]
        if metadata.get("separate_project_annotations") and kind == "official_reference":
            # 整理包中的中文说明不是官方引文；来源标题和定位保留为项目注释。
            if project_section or re.search(r"[\u4e00-\u9fff]", paragraph):
                kind = "project_policy"
        start, end = match.start(), match.start() + len(paragraph)
        while start < end:
            stop = min(start + 2400, end)
            text = content[start:stop]
            result.append(
                {
                    "id": new_id("chunk"),
                    "text": text,
                    "text_hash": text_hash(text),
                    "source_kind": kind,
                    "locator": {
                        "section": section,
                        "source_locator": source_locator,
                        "char_start": start,
                        "char_end": stop,
                        "line_start": content.count("\n", 0, start) + 1,
                        "line_end": content.count("\n", 0, stop) + 1,
                    },
                }
            )
            if stop == end:
                break
            start = stop - 200
    return result


class DocumentService:
    """
    作用：保存原文修订并排队索引
    输入：知识仓储
    输出：文档服务
    """

    def __init__(self, store):
        """
        作用：固定仓储
        输入：store - 知识仓储
        输出：无
        """
        self.store = store

    def import_document(self, request, material_key=None):
        """
        作用：以乐观锁追加修订，不覆盖旧原文
        输入：导入请求及可选内部种子键
        输出：原文与索引任务回执
        """
        data = request.model_dump(mode="json")
        if not data["content"].strip() or "\x00" in data["content"]:
            raise DataError("INVALID_DOCUMENT")
        with self.store.write() as connection:
            current = None
            if material_key:
                current = (
                    connection.execute(
                        select(m.documents).where(m.documents.c.material_key == material_key)
                    )
                    .mappings()
                    .first()
                )
            elif data["document_id"]:
                current = self.store.get(connection, m.documents, data["document_id"])
            doc_id = current["id"] if current else new_id("doc")
            version = current["version"] if current else 0
            if current and material_key:
                old = self.store.get(connection, m.revisions, current["current_revision_id"])
                if (
                    old["content_hash"] == text_hash(data["content"])
                    and old["metadata"] == data["metadata"]
                ):
                    job = connection.execute(
                        select(m.jobs.c.id).where(m.jobs.c.idempotency_key == old["id"])
                    ).scalar_one()
                    return {"document_id": doc_id, "revision_id": old["id"], "job_id": job}
            if data["expected_version"] != version:
                raise DataError("VERSION_CONFLICT", {"current_version": version})
            revision_id = new_id("docrev")
            if not current:
                connection.execute(
                    m.documents.insert().values(
                        id=doc_id,
                        title=data["title"],
                        material_key=material_key,
                        version=1,
                        current_revision_id=revision_id,
                    )
                )
            else:
                connection.execute(
                    m.documents.update()
                    .where(m.documents.c.id == doc_id)
                    .values(
                        title=data["title"], version=version + 1, current_revision_id=revision_id
                    )
                )
            connection.execute(
                m.revisions.insert().values(
                    id=revision_id,
                    document_id=doc_id,
                    version=version + 1,
                    content=data["content"],
                    content_hash=text_hash(data["content"]),
                    metadata=data["metadata"],
                    created_at=utc_now(),
                )
            )
            job_id = self.store.enqueue(
                connection, "document", {"revision_id": revision_id}, revision_id
            )
            return {"document_id": doc_id, "revision_id": revision_id, "job_id": job_id}

    def index_revision(self, revision_id, job_id=None):
        """
        作用：事务化写入分块和 FTS，重复处理不产生副本
        输入：固定修订 ID 与可选任务 ID
        输出：分块数量
        """
        with self.store.engine.connect() as connection:
            revision = self.store.get(connection, m.revisions, revision_id)
            existing = list(
                connection.execute(
                    select(m.chunks.c.id).where(m.chunks.c.revision_id == revision_id)
                ).scalars()
            )
            if existing:
                self.refresh_text_index(revision_id)
                return len(existing)
        pieces = split_document(revision["content"], revision["metadata"])
        if len(pieces) > 2000:
            raise DataError("DOCUMENT_CHUNK_LIMIT")
        if job_id:
            with self.store.write() as connection:
                connection.execute(
                    m.jobs.update().where(m.jobs.c.id == job_id).values(stage="indexing")
                )
        with self.store.write() as connection:
            existing = list(
                connection.execute(
                    select(m.chunks.c.id).where(m.chunks.c.revision_id == revision_id)
                ).scalars()
            )
            if existing:
                return len(existing)
            for piece in pieces:
                connection.execute(m.chunks.insert().values(**piece, revision_id=revision_id))
                connection.exec_driver_sql(
                    "INSERT INTO knowledge_fts(chunk_id, body) VALUES (?, ?)",
                    (piece["id"], index_text(piece["locator"]["section"] + "\n" + piece["text"])),
                )
            return len(pieces)

    def refresh_text_index(self, revision_id):
        """
        作用：补齐已保存片段的标题上下文，不改变原文、片段或旧引用
        输入：真实文档修订 ID
        输出：无
        """
        with self.store.write() as connection:
            for piece in connection.execute(
                select(m.chunks).where(m.chunks.c.revision_id == revision_id)
            ).mappings():
                connection.exec_driver_sql(
                    "DELETE FROM knowledge_fts WHERE chunk_id=?", (piece["id"],)
                )
                connection.exec_driver_sql(
                    "INSERT INTO knowledge_fts(chunk_id,body) VALUES (?,?)",
                    (piece["id"], index_text(piece["locator"]["section"] + "\n" + piece["text"])),
                )
