"""阶段 2 元数据；原文与修订不覆盖，指针和任务状态允许更新。"""

import sqlalchemy as sa

from metric_pilot.database import Base

metadata = Base.metadata
documents = sa.Table(
    "knowledge_documents",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("title", sa.String, nullable=False),
    sa.Column("material_key", sa.String, unique=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("current_revision_id", sa.String, nullable=False),
)
revisions = sa.Table(
    "document_revisions",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("document_id", sa.String, sa.ForeignKey(documents.c.id), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("content_hash", sa.String, nullable=False),
    sa.Column("metadata", sa.JSON, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.UniqueConstraint("document_id", "version"),
)
chunks = sa.Table(
    "knowledge_chunks",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("revision_id", sa.String, sa.ForeignKey(revisions.c.id), nullable=False, index=True),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("text_hash", sa.String, nullable=False),
    sa.Column("source_kind", sa.String, nullable=False),
    sa.Column("locator", sa.JSON, nullable=False),
)
embeddings = sa.Table(
    "knowledge_embeddings",
    metadata,
    sa.Column("chunk_id", sa.String, sa.ForeignKey(chunks.c.id), primary_key=True),
    sa.Column("model_key", sa.String, primary_key=True),
    sa.Column("text_hash", sa.String, nullable=False),
    sa.Column("dimension", sa.Integer, nullable=False),
    sa.Column("vector", sa.LargeBinary, nullable=False),
)
pages = sa.Table(
    "wiki_pages",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("slug", sa.String, nullable=False, unique=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("current_revision_id", sa.String, nullable=False),
    sa.Column("published_revision_id", sa.String),
)
wiki_revisions = sa.Table(
    "wiki_revisions",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("page_id", sa.String, sa.ForeignKey(pages.c.id), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.UniqueConstraint("page_id", "version"),
)
metrics = sa.Table(
    "knowledge_metrics",
    metadata,
    sa.Column("key", sa.String, primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("current_revision_id", sa.String, nullable=False),
    sa.Column("approved_revision_id", sa.String),
)
metric_revisions = sa.Table(
    "metric_revisions",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("metric_key", sa.String, sa.ForeignKey(metrics.c.key), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("definition", sa.JSON, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
    sa.UniqueConstraint("metric_key", "version"),
)
snapshots = sa.Table(
    "knowledge_snapshots",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("created_at", sa.String, nullable=False),
)
jobs = sa.Table(
    "knowledge_jobs",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("state", sa.String, nullable=False, index=True),
    sa.Column("stage", sa.String, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("result", sa.JSON, nullable=False),
    sa.Column("error_code", sa.String),
    sa.Column("idempotency_key", sa.String, unique=True),
    sa.Column("created_at", sa.String, nullable=False),
)
TABLES = [
    documents,
    revisions,
    chunks,
    embeddings,
    pages,
    wiki_revisions,
    metrics,
    metric_revisions,
    snapshots,
    jobs,
]
