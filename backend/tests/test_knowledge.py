"""知识阶段的真实 SQLite、引用、版本、适用期与检索验收。"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from metric_pilot.data.common import DataError
from metric_pilot.database import create_metadata_engine
from metric_pilot.knowledge import models as m
from metric_pilot.knowledge.contracts import (
    DocumentImport,
    DocumentMetadata,
    GenerateWiki,
    MetricWrite,
    SearchRequest,
    VersionAction,
)
from metric_pilot.knowledge.documents import DocumentService, split_document
from metric_pilot.knowledge.jobs import KnowledgeWorker
from metric_pilot.knowledge.metrics import MetricService
from metric_pilot.knowledge.provider import KnowledgeProvider
from metric_pilot.knowledge.retrieval import Retriever
from metric_pilot.knowledge.store import KnowledgeStore
from metric_pilot.knowledge.wiki import WikiService
from metric_pilot.main import create_app
from sqlalchemy import select
from test_foundation import migrate, settings


class FakeKnowledgeProvider(KnowledgeProvider):
    """
    作用：测试中替换远程传输，运行真实知识存储和校验路径
    输入：隔离配置
    输出：可计数的模型响应
    """

    calls = 0
    generation_calls = 0

    def embed(self, texts):
        """
        作用：提供仅供测试的固定向量空间
        输入：文本批次
        输出：测试向量
        """
        self.calls += 1
        return [
            [float("ArrDelay" in t or "提前" in t), float("Cancelled" in t), 0.1] for t in texts
        ]

    def generate(self, chunks, feedback=None):
        """
        作用：提供真实输入引用以验证生成落库路径
        输入：服务端固定片段
        输出：Wiki 候选和测试用量
        """
        c = chunks[0]
        self.generation_calls += 1
        return {
            "title": "测试 Wiki",
            "content": f"根据资料说明提前到达。[{c['id']}]",
            "citations": [{"chunk_id": c["id"], "excerpt": c["text"], "text_hash": c["text_hash"]}],
        }, {}


@pytest.fixture
def knowledge(tmp_path, monkeypatch):
    """
    作用：建立真实迁移后的隔离知识库
    输入：临时目录与环境隔离
    输出：仓储和测试模型入口
    """
    migrate(monkeypatch, tmp_path)
    config = settings(
        tmp_path,
        embedding_base_url="http://127.0.0.1:1/v1",
        embedding_api_key="fake",
        embedding_model="fake-embedding",
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="fake",
        llm_model="fake-generation",
    )
    engine = create_metadata_engine(config)
    store = KnowledgeStore(engine)
    try:
        yield store, FakeKnowledgeProvider(config)
    finally:
        engine.dispose()


def add_document(store, content="ArrDelay: Early arrivals show negative numbers.", **kwargs):
    """
    作用：经实际服务导入并索引测试文档
    输入：仓储、原文和请求覆盖
    输出：导入回执
    """
    values = dict(
        title="测试原文", content=content, metadata=DocumentMetadata(source_kind="project_policy")
    )
    values.update(kwargs)
    receipt = DocumentService(store).import_document(DocumentImport(**values))
    DocumentService(store).index_revision(receipt["revision_id"])
    return receipt


def wiki_payload(store, receipt):
    """
    作用：从真实原文构建可核对人工草稿
    输入：仓储和文档回执
    输出：草稿字段
    """
    with store.engine.connect() as c:
        piece = dict(
            c.execute(select(m.chunks).where(m.chunks.c.revision_id == receipt["revision_id"]))
            .mappings()
            .first()
        )
    return {
        "title": "提前到达说明",
        "content": f"提前到达可为负数。[{piece['id']}]",
        "citations": [
            {"chunk_id": piece["id"], "excerpt": piece["text"], "text_hash": piece["text_hash"]}
        ],
        "input_revision_ids": [receipt["revision_id"]],
    }


def metric_definition(revision_id):
    """
    作用：提供非官方指标测试草稿
    输入：真实来源修订
    输出：指标定义
    """
    return {
        "key": "arrival_delay_rate",
        "version": 1,
        "name": "项目到达延误率",
        "authority": "project_policy",
        "review_state": "draft",
        "unit": "percent",
        "grain": "carrier_id",
        "numerator_description": "有效到达中 ArrDel15=1 的记录数 × 100",
        "denominator_description": "有效到达数",
        "population_filters": ["cancelled=0", "diverted=0"],
        "required_columns": ["arrival_delayed_15"],
        "null_policy": "零分母 null",
        "effective_from": "2026-01-01",
        "effective_to": "2026-02-01",
        "source_revision_ids": [revision_id],
        "template_key": None,
    }


def test_document_revisions_and_stale_update(knowledge):
    """
    作用：证明更新不改变旧原文，过期版本不能覆盖
    输入：真实隔离知识库
    输出：无
    """
    store, _ = knowledge
    first = add_document(store)
    second = add_document(
        store,
        "Cancelled: Cancelled Flight Indicator.",
        document_id=first["document_id"],
        expected_version=1,
    )
    with store.engine.connect() as c:
        assert store.get(c, m.revisions, first["revision_id"])["content"].startswith("ArrDelay")
        assert store.get(c, m.revisions, second["revision_id"])["version"] == 2
    with pytest.raises(DataError, match="VERSION_CONFLICT"):
        add_document(store, "stale", document_id=first["document_id"], expected_version=1)


def test_exact_chunk_coordinates_and_authority(knowledge):
    """
    作用：验证混合注释不会被标成官方原文且长段坐标精确
    输入：隔离知识库
    输出：无
    """
    store, _ = knowledge
    text = (
        "# ArrDelay\n\nEarly arrivals show negative numbers.\n\n项目映射：arrival_delay。\n\n"
        + "x" * 6000
    )
    metadata = DocumentMetadata(
        source_kind="official_reference",
        origin_url="https://example.org/dictionary",
        separate_project_annotations=True,
    )
    receipt = add_document(store, text, metadata=metadata)
    with store.engine.connect() as c:
        chunks = list(
            c.execute(
                select(m.chunks).where(m.chunks.c.revision_id == receipt["revision_id"])
            ).mappings()
        )
    for piece in chunks:
        loc = piece["locator"]
        assert text[loc["char_start"] : loc["char_end"]] == piece["text"]
        assert len(piece["text"]) <= 2400
    assert (
        next(p for p in chunks if p["text"].startswith("Early"))["source_kind"]
        == "official_reference"
    )
    assert (
        next(p for p in chunks if p["text"].startswith("项目映射"))["source_kind"]
        == "project_policy"
    )
    assert len(split_document("字段含义：提前到达。", {"source_kind": "official_reference"})) == 1


def test_chinese_search_snapshot_and_applicability(knowledge):
    """
    作用：验证中文命中英文、快照不漂移与日期过滤
    输入：隔离知识库
    输出：无
    """
    store, provider = knowledge
    first = add_document(
        store,
        metadata=DocumentMetadata(
            source_kind="project_policy", effective_from="2026-01-01", effective_to="2026-02-01"
        ),
    )
    fixed = store.snapshot()
    add_document(
        store, "Cancelled: cancellation only.", document_id=first["document_id"], expected_version=1
    )
    retriever = Retriever(store, provider)
    result = retriever.search(
        SearchRequest(query="提前到达为什么是负数", snapshot_id=fixed["id"], as_of="2026-01-15")
    )
    assert any("Early arrivals" in item["chunk"]["text"] for item in result["items"])
    assert all(item["chunk"]["revision_id"] == first["revision_id"] for item in result["items"])
    assert (
        retriever.search(
            SearchRequest(query="ArrDelay", snapshot_id=fixed["id"], as_of="2026-02-01")
        )["items"]
        == []
    )
    assert provider.calls == 0


def test_hybrid_persistence_model_isolation_and_missing_index(knowledge):
    """
    作用：验证真实持久化向量和模型空间隔离，不冒充混合检索
    输入：隔离知识库
    输出：无
    """
    store, provider = knowledge
    first = add_document(store)
    add_document(store, "Cancelled: Cancelled Flight Indicator.")
    fixed = store.snapshot()
    request = SearchRequest(query="提前到达", snapshot_id=fixed["id"], mode="hybrid")
    with pytest.raises(DataError, match="EMBEDDING_INDEX_REQUIRED"):
        Retriever(store, provider).search(request)
    Retriever(store, provider).index_embeddings(fixed["document_revision_ids"])
    calls = provider.calls
    Retriever(store, provider).index_embeddings(fixed["document_revision_ids"])
    assert provider.calls == calls
    # 新实例从数据库读取，不依赖旧检索器内存。
    result = Retriever(KnowledgeStore(store.engine), provider).search(request)
    assert (
        result["mode"] == "hybrid"
        and result["items"][0]["chunk"]["revision_id"] == first["revision_id"]
    )
    provider.settings.embedding_model = "another-test-model"
    with pytest.raises(DataError, match="EMBEDDING_INDEX_REQUIRED"):
        Retriever(store, provider).search(request)


@pytest.mark.parametrize("bad", ["id", "excerpt", "hash", "scope", "paragraph"])
def test_invalid_wiki_citations_rejected(knowledge, bad):
    """
    作用：拒绝伪造引用、跨输入引用和缺少段落引用
    输入：隔离库及破坏类型
    输出：无
    """
    store, provider = knowledge
    receipt = add_document(store)
    payload = wiki_payload(store, receipt)
    if bad == "id":
        payload["citations"][0]["chunk_id"] = "chunk_nonexistent"
    if bad == "excerpt":
        payload["citations"][0]["excerpt"] = "原文没有这句话"
    if bad == "hash":
        payload["citations"][0]["text_hash"] = "invented"
    if bad == "scope":
        payload["input_revision_ids"] = [add_document(store, "other")["revision_id"]]
    if bad == "paragraph":
        payload["content"] += "\n\n没有来源的新结论。"
    with pytest.raises(DataError):
        WikiService(store, provider).save("arrivals", payload, 0)


def test_wiki_publish_edit_rollback_and_generation_conflict(knowledge):
    """
    作用：验证发布、人工修改、非破坏回滚和生成基线冲突
    输入：真实知识库
    输出：无
    """
    store, provider = knowledge
    receipt = add_document(store)
    wiki = WikiService(store, provider)
    first = wiki.save("arrivals", wiki_payload(store, receipt), 0)
    published = wiki.action(
        first["page_id"], VersionAction(revision_id=first["id"], expected_version=1), True
    )
    fixed = store.snapshot()
    assert fixed["wiki_revision_ids"] == [published["id"]]
    result = Retriever(store, provider).search(
        SearchRequest(query="提前到达", snapshot_id=fixed["id"])
    )
    assert result["wiki_items"][0]["id"] == published["id"]
    request = GenerateWiki(
        slug="arrivals", input_revision_ids=[receipt["revision_id"]], expected_version=2
    )
    job1 = wiki.queue_generation(request)
    assert wiki.queue_generation(request) == job1
    payload = wiki_payload(store, receipt)
    payload["content"] = "人工编辑保留。" + payload["content"]
    edited = wiki.save("arrivals", payload, 2)
    worker = KnowledgeWorker(store, provider)
    while worker.step():
        pass
    with store.engine.connect() as c:
        assert store.get(c, m.jobs, job1)["error_code"] == "VERSION_CONFLICT"
        assert store.get(c, m.pages, first["page_id"])["current_revision_id"] == edited["id"]
        assert store.get(c, m.wiki_revisions, published["id"])["payload"]["state"] == "published"
    rolled = wiki.action(
        first["page_id"], VersionAction(revision_id=first["id"], expected_version=3), False
    )
    assert rolled["version"] == 4 and rolled["payload"]["state"] == "draft"
    assert rolled["payload"]["content"] == first["payload"]["content"]
    with pytest.raises(DataError, match="VERSION_CONFLICT"):
        wiki.save("arrivals", payload, 2)


def test_generated_wiki_is_only_a_draft(knowledge):
    """
    作用：验证生成路径调用模型适配器并保存真实引用，不自动发布
    输入：隔离知识库
    输出：无
    """
    store, provider = knowledge
    receipt = add_document(store)
    wiki = WikiService(store, provider)
    job = wiki.queue_generation(
        GenerateWiki(slug="generated", input_revision_ids=[receipt["revision_id"]])
    )
    worker = KnowledgeWorker(store, provider)
    while worker.step():
        pass
    with store.engine.connect() as c:
        record = store.get(c, m.jobs, job)
        assert record["state"] == "succeeded"
        revision = store.get(c, m.wiki_revisions, record["result"]["revision_id"])
        assert revision["payload"]["edit_source"] == "model"
        assert revision["payload"]["state"] == "draft"
    assert store.snapshot()["wiki_revision_ids"] == []


def test_metric_review_dates_and_immutable_snapshot(knowledge):
    """
    作用：验证指标不会自动批准，版本和日期绑定不漂移
    输入：隔离知识库
    输出：无
    """
    store, _ = knowledge
    receipt = add_document(store)
    service = MetricService(store)
    draft = service.save(MetricWrite(definition=metric_definition(receipt["revision_id"])))
    before = store.snapshot()
    with pytest.raises(DataError, match="METRIC_NOT_APPLICABLE"):
        service.resolve(before["id"], "arrival_delay_rate", "2026-01-15")
    approved = service.approve(
        "arrival_delay_rate", VersionAction(revision_id=draft["id"], expected_version=1)
    )
    fixed = store.snapshot()
    assert service.resolve(fixed["id"], "arrival_delay_rate", "2026-01-15")["id"] == approved["id"]
    with pytest.raises(DataError, match="METRIC_NOT_APPLICABLE"):
        service.resolve(fixed["id"], "arrival_delay_rate", "2026-02-01")
    changed = {
        **approved["definition"],
        "review_state": "draft",
        "version": 3,
        "denominator_description": "另一个明确的新口径",
    }
    service.save(MetricWrite(definition=changed, expected_version=2))
    assert (
        service.resolve(fixed["id"], "arrival_delay_rate", "2026-01-15")["definition"][
            "denominator_description"
        ]
        == "有效到达数"
    )
    with pytest.raises(DataError, match="METRIC_APPROVAL_REQUIRED"):
        service.save(MetricWrite(definition=approved["definition"], expected_version=3))


def test_metric_authority_scope_and_conflict(knowledge):
    """
    作用：拒绝项目规则冒充官方、越期来源及多个固定候选
    输入：隔离知识库
    输出：无
    """
    store, _ = knowledge
    receipt = add_document(
        store,
        metadata=DocumentMetadata(
            source_kind="project_policy", effective_from="2026-01-01", effective_to="2026-02-01"
        ),
    )
    definition = metric_definition(receipt["revision_id"])
    service = MetricService(store)
    with pytest.raises(DataError, match="METRIC_AUTHORITY_MISMATCH"):
        service.save(MetricWrite(definition={**definition, "authority": "official_mapping"}))
    with pytest.raises(DataError, match="METRIC_SOURCE_DATE_MISMATCH"):
        service.save(MetricWrite(definition={**definition, "effective_to": "2026-03-01"}))
    draft = service.save(MetricWrite(definition=definition))
    first = service.approve(
        definition["key"], VersionAction(revision_id=draft["id"], expected_version=1)
    )
    second_draft = service.save(
        MetricWrite(definition={**definition, "version": 3}, expected_version=2)
    )
    second = service.approve(
        definition["key"], VersionAction(revision_id=second_draft["id"], expected_version=3)
    )
    fixed = store.snapshot()
    # 模拟固定清单中明确请求保留两个冲突修订，解析器必须拒绝选猜。
    with store.write() as c:
        snapshot = store.get(c, m.snapshots, fixed["id"])
        c.execute(
            m.snapshots.update()
            .where(m.snapshots.c.id == fixed["id"])
            .values(
                payload={**snapshot["payload"], "metric_revision_ids": [first["id"], second["id"]]}
            )
        )
    with pytest.raises(DataError, match="METRIC_CONFLICT"):
        service.resolve(fixed["id"], definition["key"], "2026-01-15")


def test_upload_api_limits_job_and_missing_models(tmp_path, monkeypatch):
    """
    作用：验证真实上传/任务接口和未配置态，拒绝无效编码与越界引用
    输入：临时目录与环境隔离
    输出：无
    """
    migrate(monkeypatch, tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        form = {"title": "真实上传测试", "metadata": json.dumps({"source_kind": "project_policy"})}
        response = client.post(
            "/api/v1/documents",
            data=form,
            files={
                "file": (
                    "guide.md",
                    "ArrDelay: Early arrivals show negative numbers.",
                    "text/markdown",
                )
            },
        )
        assert response.status_code == 202
        receipt = response.json()
        assert client.get(f"/api/v1/jobs/{receipt['job_id']}").status_code == 200
        documents = client.get("/api/v1/documents").json()["items"]
        assert documents[0]["title"] == "真实上传测试"
        assert "payload" not in client.get(f"/api/v1/jobs/{receipt['job_id']}").json()
        assert (
            client.post("/api/v1/knowledge/reindex").json()["error"]["code"]
            == "MODEL_NOT_CONFIGURED"
        )
        assert (
            client.post(
                "/api/v1/wiki/generate",
                json={
                    "slug": "guide",
                    "input_revision_ids": [receipt["revision_id"]],
                    "expected_version": 0,
                },
            ).json()["error"]["code"]
            == "MODEL_NOT_CONFIGURED"
        )
        for name, body in [("bad.pdf", b"fake"), ("bad.txt", b"\xff"), ("large.md", b"x" * 500001)]:
            assert (
                client.post(
                    "/api/v1/documents", data=form, files={"file": (name, body)}
                ).status_code
                == 409
            )
        assert (
            client.get(f"/api/v1/documents/wrong/revisions/{receipt['revision_id']}").json()[
                "error"
            ]["code"]
            == "REVISION_SCOPE_MISMATCH"
        )


@pytest.mark.parametrize("recover", [True, False])
def test_generation_repair_is_bounded_and_keeps_validation(knowledge, monkeypatch, recover):
    """
    作用：引用错误最多修正一次，重复失败不落库
    输入：隔离库、替换函数与是否恢复
    输出：无
    """
    store, provider = knowledge
    receipt = add_document(store)
    original = provider.generate
    calls = []

    def generated(pieces, feedback=None):
        """
        作用：返回一次或持续伪造引用以验证重试上限
        输入：片段和反馈
        输出：测试候选
        """
        candidate, usage = original(pieces)
        calls.append(feedback)
        if len(calls) == 1 or not recover:
            candidate["content"] = "没有合法引用。"
        return candidate, usage

    monkeypatch.setattr(provider, "generate", generated)
    service = WikiService(store, provider)
    payload = {
        "slug": "repair",
        "input_revision_ids": [receipt["revision_id"]],
        "expected_version": 0,
    }
    if recover:
        result = service.generate(payload)
        assert len(result["usage"]["attempts"]) == 2
    else:
        with pytest.raises(DataError, match="WIKI_CITATIONS_REQUIRED"):
            service.generate(payload)
        with store.engine.connect() as connection:
            assert not connection.execute(select(m.pages)).first()
    assert calls == [None, "WIKI_CITATIONS_REQUIRED"]


def test_provider_endpoint_and_json_request(knowledge, monkeypatch):
    """
    作用：完整端点不重复拼接，生成约束 JSON 且不保存内部思维字段
    输入：隔离配置与 HTTP mock
    输出：无
    """
    _, fake = knowledge
    fake.settings.embedding_base_url = "https://example.org/v1/embeddings"
    provider = KnowledgeProvider(fake.settings)
    requests = []

    def respond(request):
        """
        作用：模拟兼容 HTTP 端点，所有数据仅为测试值
        输入：HTTP 请求
        输出：模拟 HTTP 响应
        """
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.2]}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"title": "test", "content": "test", "citations": []}
                            ),
                            "reasoning_content": "test-private-reasoning-do-not-persist",
                        },
                    }
                ],
                "usage": {"total_tokens": 12},
            },
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert provider.embed(["text"]) == [[1.0, 0.2]]
    assert requests[0].url.path == "/v1/embeddings"
    candidate, usage = provider.generate([])
    assert candidate["title"] == "test" and usage["total_tokens"] == 12
    body = json.loads(requests[1].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "none"
    assert "test-private-reasoning" not in json.dumps(candidate)


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 1, "embedding": [1.0]}],
        [{"index": 0, "embedding": [float("nan")]}],
        [{"index": 0, "embedding": [0.0, 0.0]}],
    ],
)
def test_invalid_embedding_responses(knowledge, monkeypatch, data):
    """
    作用：拒绝错序、非有限或零向量
    输入：隔离配置和非法响应
    输出：无
    """
    _, fake = knowledge
    provider = KnowledgeProvider(fake.settings)
    monkeypatch.setattr(provider, "post", lambda *args: {"data": data})
    with pytest.raises(DataError, match="INVALID_EMBEDDING_RESPONSE"):
        provider.embed(["text"])
