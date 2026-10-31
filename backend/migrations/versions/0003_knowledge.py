"""阶段 2 冻结迁移；不导入可继续演化的运行模型。"""

from alembic import op

revision = "0003_knowledge"
down_revision = "0002_data"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE knowledge_documents (
	id VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	material_key VARCHAR, 
	version INTEGER NOT NULL, 
	current_revision_id VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (material_key)
)""",
    """CREATE TABLE document_revisions (
	id VARCHAR NOT NULL, 
	document_id VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	content_hash VARCHAR NOT NULL, 
	metadata JSON NOT NULL, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (document_id, version), 
	FOREIGN KEY(document_id) REFERENCES knowledge_documents (id)
)""",
    """CREATE TABLE knowledge_chunks (
	id VARCHAR NOT NULL, 
	revision_id VARCHAR NOT NULL, 
	text TEXT NOT NULL, 
	text_hash VARCHAR NOT NULL, 
	source_kind VARCHAR NOT NULL, 
	locator JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(revision_id) REFERENCES document_revisions (id)
)""",
    """CREATE INDEX ix_knowledge_chunks_revision_id ON knowledge_chunks (revision_id)""",
    """CREATE TABLE knowledge_embeddings (
	chunk_id VARCHAR NOT NULL, 
	model_key VARCHAR NOT NULL, 
	text_hash VARCHAR NOT NULL, 
	dimension INTEGER NOT NULL, 
	vector BLOB NOT NULL, 
	PRIMARY KEY (chunk_id, model_key), 
	FOREIGN KEY(chunk_id) REFERENCES knowledge_chunks (id)
)""",
    """CREATE TABLE wiki_pages (
	id VARCHAR NOT NULL, 
	slug VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	current_revision_id VARCHAR NOT NULL, 
	published_revision_id VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
)""",
    """CREATE TABLE wiki_revisions (
	id VARCHAR NOT NULL, 
	page_id VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	payload JSON NOT NULL, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (page_id, version), 
	FOREIGN KEY(page_id) REFERENCES wiki_pages (id)
)""",
    """CREATE TABLE knowledge_metrics (
	"key" VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	current_revision_id VARCHAR NOT NULL, 
	approved_revision_id VARCHAR, 
	PRIMARY KEY ("key")
)""",
    """CREATE TABLE metric_revisions (
	id VARCHAR NOT NULL, 
	metric_key VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	definition JSON NOT NULL, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (metric_key, version), 
	FOREIGN KEY(metric_key) REFERENCES knowledge_metrics ("key")
)""",
    """CREATE TABLE knowledge_snapshots (
	id VARCHAR NOT NULL, 
	payload JSON NOT NULL, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (id)
)""",
    """CREATE TABLE knowledge_jobs (
	id VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	stage VARCHAR NOT NULL, 
	payload JSON NOT NULL, 
	result JSON NOT NULL, 
	error_code VARCHAR, 
	idempotency_key VARCHAR, 
	created_at VARCHAR NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (idempotency_key)
)""",
    """CREATE INDEX ix_knowledge_jobs_state ON knowledge_jobs (state)""",
    """CREATE VIRTUAL TABLE knowledge_fts USING fts5(chunk_id UNINDEXED, body)""",
)


def upgrade():
    """
    作用：按冻结 DDL 创建知识表及 FTS 索引
    输入：无
    输出：无
    """
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    """
    作用：撤销知识元数据，不改动航班快照文件
    输入：无
    输出：无
    """
    op.execute("DROP TABLE knowledge_fts")
    op.drop_table("knowledge_jobs")
    op.drop_table("knowledge_snapshots")
    op.drop_table("metric_revisions")
    op.drop_table("knowledge_metrics")
    op.drop_table("wiki_revisions")
    op.drop_table("wiki_pages")
    op.drop_table("knowledge_embeddings")
    op.drop_table("knowledge_chunks")
    op.drop_table("document_revisions")
    op.drop_table("knowledge_documents")
