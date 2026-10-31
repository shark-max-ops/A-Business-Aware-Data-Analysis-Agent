import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Drawer, Empty, Input, Modal, Select, Space, Table, Tabs, Tag } from 'antd';
import { knowledgeApi as api, jsonRequest as json } from '../api/knowledge';
import type { Chunk, DocumentItem, Job, MetricDefinition, MetricItem, MetricRevision, Revision, SearchResult, Snapshot, WikiPage, WikiPayload, WikiRevision } from '../api/knowledge';
import { ReadableMarkdown } from './ReadableMarkdown';
import { KnowledgeGraph } from './KnowledgeGraph';
import { MetricSummary } from './MetricSummary';
import { fieldLabels } from './knowledgePresentation';

const kindLabel = (kind: string) => kind === 'official_reference' ? '官方资料' : kind === 'generated_wiki' ? '生成 Wiki' : '项目规则/整理提示';
const stateLabel = (state: string) => ({ queued: '排队', running: '处理中', succeeded: '完成', failed: '失败', draft: '待审核草稿', published: '已发布', approved: '已审核' }[state] ?? state);

/** 作用：展示可操作知识管理页和固定检索范围；输入：无；输出：知识页面。 */
export function KnowledgePage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [metrics, setMetrics] = useState<MetricItem[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<'text' | 'hybrid'>('text');
  const [asOf, setAsOf] = useState('2026-01-01');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [hits, setHits] = useState<SearchResult | null>(null);
  const [references, setReferences] = useState<Chunk[]>([]);
  const [raw, setRaw] = useState<Revision | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [history, setHistory] = useState<Revision[]>([]);
  const [source, setSource] = useState<Chunk | null>(null);
  const [activeTab, setActiveTab] = useState('documents');
  const [metricFocus, setMetricFocus] = useState({ key: '', version: 0 });
  const [showAllHits, setShowAllHits] = useState(false);

  const refresh = useCallback(async () => {
    const [d, w, m, j] = await Promise.all([
      api<{ items: DocumentItem[] }>('/documents'), api<{ items: WikiPage[] }>('/wiki/pages'),
      api<{ items: MetricItem[] }>('/metrics'), api<{ items: Job[] }>('/jobs'),
    ]);
    setDocuments(d.items); setPages(w.items); setMetrics(m.items); setJobs(j.items);
  }, []);

  useEffect(() => { void refresh().catch(e => setError(String(e.message))); }, [refresh]);
  useEffect(() => {
    if (!jobs.some(j => j.state === 'queued' || j.state === 'running')) return;
    const timer = setInterval(() => { void refresh().catch(e => setError(String(e.message))); }, 1500);
    return () => clearInterval(timer);
  }, [jobs, refresh]);

  /** 作用：管理操作进行态及真实失败；输入：异步操作；输出：完成状态。 */
  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  };

  /** 作用：读取固定原文、历史和分块；输入：文档及修订；输出：来源抽屉状态。 */
  const showDocument = async (documentId: string, revisionId: string) => {
    await perform(async () => {
      const [revision, pieces, revisions] = await Promise.all([
        api<Revision>(`/documents/${documentId}/revisions/${revisionId}`),
        api<{ items: Chunk[] }>(`/documents/${documentId}/revisions/${revisionId}/chunks`),
        api<{ items: Revision[] }>(`/documents/${documentId}/revisions`),
      ]);
      setRaw(revision); setChunks(pieces.items); setHistory(revisions.items);
    });
  };

  /** 作用：从引用继续查看真实原文；输入：片段 ID；输出：来源抽屉。 */
  const inspect = async (id: string) => {
    await perform(async () => {
      const chunk = await api<Chunk>(`/chunks/${id}`); setSource(chunk);
      const revisions = documents.flatMap(d => [{ documentId: d.id }]);
      // 片段可能属于旧原文，先从各文档历史定位，不以当前版本替换。
      for (const item of revisions) {
        const h = await api<{ items: Revision[] }>(`/documents/${item.documentId}/revisions`);
        const r = h.items.find(v => v.id === chunk.revision_id);
        if (r) { setRaw(r); setHistory(h.items); setChunks([chunk]); return; }
      }
    });
  };

  /** 作用：从指标来源打开固定文档版本；输入：原文修订；输出：来源抽屉。 */
  const openRevision = async (revisionId: string) => {
    const current = documents.find(d => d.current_revision_id === revisionId);
    if (current) return showDocument(current.id, revisionId);
    await perform(async () => {
      for (const doc of documents) {
        const versions = await api<{ items: Revision[] }>(`/documents/${doc.id}/revisions`);
        const revision = versions.items.find(r => r.id === revisionId);
        if (revision) { setRaw(revision); setHistory(versions.items); setChunks([]); return; }
      }
      throw new Error('没有找到该固定来源版本');
    });
  };

  return <>
    <div className="page-heading"><div><p className="eyebrow">KNOWLEDGE & METRICS</p><h1>为每个指标建立依据</h1><p>原文、Wiki 和指标分别保存；发布与审核由你明确操作。</p></div><Button onClick={() => void perform(refresh)} loading={busy}>刷新目录</Button></div>
    {error && <Alert showIcon type="error" message="操作未完成" description={error} closable onClose={() => setError('')} />}
    {notice && <Alert showIcon type="success" message={notice} closable onClose={() => setNotice('')} />}
    <section className="panel knowledge-search">
      <h2>查阅固定版本的知识</h2>
      <Space wrap>
        <Input aria-label="知识问题" value={query} onChange={e => setQuery(e.target.value)} placeholder="例如：提前到达为什么是负数" style={{ width: 300, maxWidth: '100%' }} />
        <Select aria-label="检索模式" value={mode} onChange={setMode} options={[{ value: 'text', label: '关键词检索' }, { value: 'hybrid', label: '关键词 + 向量' }]} />
        <label>数据日期 <Input aria-label="知识适用日期" type="date" value={asOf} onChange={e => setAsOf(e.target.value)} /></label>
        <Button aria-label="检索" loading={busy} disabled={!query.trim()} onClick={() => void perform(async () => {
          setHits(null); setShowAllHits(false);
          const fixed = snapshot ?? await api<Snapshot>('/knowledge/snapshots', { method: 'POST' });
          setSnapshot(fixed); setHits(await api<SearchResult>('/knowledge/search', json({ query, mode, snapshot_id: fixed.id, as_of: asOf || null })));
        })}>检索</Button>
        <Button onClick={() => void perform(async () => { setSnapshot(await api<Snapshot>('/knowledge/snapshots', { method: 'POST' })); setHits(null); setNotice('已固定当前知识版本，之后的编辑不会改变本次检索范围'); })}>更新知识快照</Button>
        <Button onClick={() => void perform(async () => { const job = await api<Job>('/knowledge/reindex', { method: 'POST' }); setNotice(`向量构建已排队：${job.id}`); })}>构建/更新向量索引</Button>
      </Space>
      <p className="muted">{snapshot ? `固定范围：${snapshot.document_revision_ids.length} 个文档修订，${snapshot.wiki_revision_ids.length} 个已发布 Wiki，${snapshot.metric_revision_ids.length} 个已审核指标` : '首次检索时固定当前版本。更新资料后，可手动更新知识快照。'}；所选引用 {references.length} 条。</p>
      {hits && <div className="search-result-area"><p>实际模式：{hits.mode === 'hybrid' ? '关键词 + 向量' : '关键词检索'} · 找到 {hits.items.length} 个相关主题，先看最相关的依据</p>{hits.warnings.length > 0 && <details className="technical-details"><summary>资料适用期提醒</summary>{hits.warnings.map(w => <p key={w}>{w}</p>)}</details>}{hits.items.length === 0 && <Empty description="当前版本与日期下没有匹配片段" />}{hits.items.slice(0, showAllHits ? undefined : 5).map((hit, index) => <article className="knowledge-hit" key={hit.chunk.id}>
        <Space wrap><span className="search-rank">{index + 1}</span><Tag color={hit.chunk.source_kind === 'official_reference' ? 'green' : 'gold'}>{kindLabel(hit.chunk.source_kind)}</Tag><strong>{fieldLabels[String(hit.chunk.locator.section)] ?? String(hit.chunk.locator.section)}</strong></Space>
        <p className="hit-context">{hit.match_reason} · {documents.find(d => d.current_revision_id === hit.chunk.revision_id)?.title ?? '固定历史资料'} · 原文第 {String(hit.chunk.locator.line_start)} 行</p>
        <ReadableMarkdown text={hit.excerpt || hit.chunk.text} />
        {hit.excerpt && hit.excerpt !== hit.chunk.text && <details className="technical-details"><summary>展开完整原文片段</summary><ReadableMarkdown text={hit.chunk.text} /></details>}
        <Space wrap><Button size="small" onClick={() => void inspect(hit.chunk.id)}>查看原文</Button><Button size="small" onClick={() => setReferences(old => old.some(c => c.id === hit.chunk.id) ? old : [...old, hit.chunk])}>选作 Wiki 引用</Button></Space>
      </article>)}{hits.items.length > 5 && <Button onClick={() => setShowAllHits(v => !v)}>{showAllHits ? '收起更多结果' : `展开其余 ${hits.items.length - 5} 个主题`}</Button>}</div>}
      {hits?.wiki_items?.map((item, index) => { const r = item as unknown as WikiRevision; return <details key={index} className="knowledge-hit"><summary>已发布 Wiki：{r.payload.title}</summary><ReadableMarkdown text={r.payload.content} citations={r.payload.citations} onCitation={id => void inspect(id)} /></details>; })}
    </section>
    <section className="panel knowledge-panel"><Tabs activeKey={activeTab} onChange={setActiveTab} items={[
      { key: 'documents', label: '原始文档', children: <DocumentsPanel documents={documents} busy={busy} perform={perform} showDocument={showDocument} notify={setNotice} /> },
      { key: 'wiki', label: 'Wiki', children: <WikiPanel metricVersion={metrics.map(m => m.current_revision_id).join("|")} pages={pages} documents={documents} references={references} busy={busy} perform={perform} inspect={inspect} notify={setNotice} openDocument={showDocument} selectMetric={key => { setMetricFocus(old => ({ key, version: old.version + 1 })); setActiveTab('metrics'); }} /> },
      { key: 'metrics', label: '指标定义', children: <MetricsPanel metrics={metrics} documents={documents} busy={busy} perform={perform} snapshot={snapshot} asOf={asOf} notify={setNotice} initialKey={metricFocus} openSource={id => void openRevision(id)} /> },
      { key: 'jobs', label: `处理任务 (${jobs.filter(j => j.state === 'queued' || j.state === 'running').length})`, children: <Table<Job> rowKey="id" dataSource={jobs} scroll={{ x: 650 }} columns={[
        { title: '类型', dataIndex: 'kind' }, { title: '状态', dataIndex: 'state', render: stateLabel }, { title: '进度', dataIndex: 'stage' },
        { title: '结果/错误', render: (_, j) => j.error_code ?? JSON.stringify(j.result) },
        { title: '操作', render: (_, j) => j.state === 'failed' ? <Button onClick={() => void perform(async () => { await api(`/jobs/${j.id}/retry`, { method: 'POST' }); })}>重试</Button> : null },
      ]} /> },
    ]} /></section>
    <Drawer title="固定原文与来源" open={!!raw} width={820} onClose={() => { setRaw(null); setSource(null); }}>
      {raw && <><Space wrap><Tag>{kindLabel(raw.metadata.source_kind)}</Tag><span>v{raw.version}</span><Select aria-label="原文历史版本" value={raw.id} style={{ width: 230 }} options={history.map(r => ({ value: r.id, label: `v${r.version} · ${r.created_at.slice(0, 10)}` }))} onChange={id => void showDocument(raw.document_id, id)} /></Space>
        <p>{raw.metadata.origin_url && <a href={raw.metadata.origin_url} target="_blank" rel="noreferrer">打开来源网址</a>}</p><p>适用期：{raw.metadata.effective_from ?? '未声明'} 至 {raw.metadata.effective_to ?? '未声明截止'}（不含）</p>
        {source && <Alert type="info" message={`引用位置：${String(source.locator.section)} · 第 ${String(source.locator.line_start)} 行`} />}
        <details className="technical-details"><summary>技术定位与哈希校验</summary><p>修订：{raw.id}</p><p>SHA-256：{raw.content_hash}</p>{source && <pre>{JSON.stringify(source.locator, null, 2)}</pre>}</details>
        <Tabs items={[{ key: 'raw', label: '原文', children: <><ReadableMarkdown text={raw.content} /><details className="technical-details"><summary>查看原始 Markdown / 文本</summary><pre className="knowledge-text">{raw.content}</pre></details></> }, { key: 'chunks', label: '可引用片段', children: chunks.map(c => <article className="knowledge-hit" key={c.id}><Tag>{kindLabel(c.source_kind)}</Tag><Button size="small" onClick={() => setReferences(old => old.some(x => x.id === c.id) ? old : [...old, c])}>选作 Wiki 引用</Button><ReadableMarkdown text={c.text} /><small>{String(c.locator.section)} · 第 {String(c.locator.line_start)} 行</small></article>) }]} />
      </>}
    </Drawer>
  </>;
}

type Actions = { busy: boolean; perform: (action: () => Promise<void>) => Promise<void>; notify: (text: string) => void };

/** 作用：导入或更新 UTF-8 原文，并查看历史；输入：目录与操作；输出：文档面板。 */
function DocumentsPanel({ documents, busy, perform, showDocument, notify }: Actions & { documents: DocumentItem[]; showDocument: (doc: string, rev: string) => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [target, setTarget] = useState<string | null>(null);
  const [targetVersion, setTargetVersion] = useState(0);
  const [kind, setKind] = useState<'official_reference' | 'project_policy'>('project_policy');
  const [url, setUrl] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  return <div className="knowledge-stack"><div className="knowledge-form">
    <label>原文文件（UTF-8 Markdown / TXT，最大 500 KB）<input aria-label="原文文件" type="file" accept=".md,.txt" onChange={e => { const selected = e.target.files?.[0] ?? null; setFile(selected); if (selected && !title) setTitle(selected.name.replace(/\.(md|txt)$/i, '')); }} /></label>
    <label>文档标题<Input aria-label="文档标题" value={title} onChange={e => setTitle(e.target.value)} /></label>
    <label>保存方式<Select aria-label="文档保存方式" value={target ?? ''} onChange={v => { setTarget(v || null); setTargetVersion(documents.find(d => d.id === v)?.version ?? 0); }} options={[{ value: '', label: '新建文档' }, ...documents.map(d => ({ value: d.id, label: `更新：${d.title}（当前 v${d.version}）` }))]} /></label>
    <label>来源类型<Select aria-label="文档来源类型" value={kind} onChange={setKind} options={[{ value: 'project_policy', label: '项目规则' }, { value: 'official_reference', label: '官方资料' }]} /></label>
    <label>原始网址<Input aria-label="文档来源网址" value={url} onChange={e => setUrl(e.target.value)} placeholder="官方资料必填 http(s) 网址" /></label>
    <label>生效日期<Input aria-label="文档生效日期" type="date" value={start} onChange={e => setStart(e.target.value)} /></label>
    <label>截止日期（不含）<Input aria-label="文档截止日期" type="date" value={end} onChange={e => setEnd(e.target.value)} /></label>
    <Button type="primary" loading={busy} disabled={!file || !title.trim() || (kind === 'official_reference' && !url)} onClick={() => void perform(async () => {
      const body = new FormData(); body.set('file', file!); body.set('title', title); body.set('expected_version', String(targetVersion));
      if (target) body.set('document_id', target);
      body.set('metadata', JSON.stringify({ source_kind: kind, origin_url: url || null, effective_from: start || null, effective_to: end || null }));
      const receipt = await api<{ job_id: string }>('/documents', { method: 'POST', body }); if (target) setTargetVersion(v => v + 1); notify(`原文已保存，处理任务：${receipt.job_id}`);
    })}>保存原文并索引</Button>
  </div><Table<DocumentItem> rowKey="id" dataSource={documents} locale={{ emptyText: '尚无文档，请导入材料' }} scroll={{ x: 650 }} columns={[
    { title: '文档', dataIndex: 'title' }, { title: '来源', render: (_, d) => kindLabel(d.metadata.source_kind) }, { title: '版本', dataIndex: 'version' },
    { title: '状态', render: (_, d) => d.job ? stateLabel(d.job.state) : '未处理' },
    { title: '操作', render: (_, d) => <Button onClick={() => void showDocument(d.id, d.current_revision_id)}>原文与历史</Button> },
  ]} /></div>;
}

/** 作用：查看和编辑有引用的 Wiki，生成只创建草稿；输入：目录与引用；输出：Wiki 面板。 */
function WikiPanel({ pages, documents, references, busy, perform, inspect, notify, openDocument, selectMetric, metricVersion }: Actions & { metricVersion: string; pages: WikiPage[]; documents: DocumentItem[]; references: Chunk[]; inspect: (id: string) => Promise<void>; openDocument: (doc: string, rev: string) => Promise<void>; selectMetric: (key: string) => void }) {
  const [modal, modalContext] = Modal.useModal();
  const [selected, setSelected] = useState<string | null>(null);
  const [editorVersion, setEditorVersion] = useState(0);
  const [slug, setSlug] = useState('');
  const [draft, setDraft] = useState<Pick<WikiPayload, 'title' | 'content' | 'citations' | 'input_revision_ids'>>({ title: '', content: '', citations: [], input_revision_ids: [] });
  const [inputs, setInputs] = useState<string[]>([]);
  const [history, setHistory] = useState<WikiRevision[]>([]);
  const [published, setPublished] = useState<WikiRevision | null>(null);
  const [initialized, setInitialized] = useState(false);
  const [editing, setEditing] = useState(true);
  const page = pages.find(p => p.id === selected);
  useEffect(() => {
    if (!initialized && pages.length) {
      const first = pages[0]; setSelected(first.id); setSlug(first.slug); setEditorVersion(first.version); setDraft({ title: first.revision.payload.title, content: first.revision.payload.content, citations: first.revision.payload.citations, input_revision_ids: first.revision.payload.input_revision_ids }); setInitialized(true); setEditing(false);
    }
  }, [pages, initialized]);
  /** 作用：发布已保存版本，保留编辑框未保存内容的边界；输入：无；输出：确认窗口。 */
  const requestPublish = () => modal.confirm({ title: '发布当前已保存草稿？', okText: '确认发布', cancelText: '取消', content: '请核对正文和引用。此操作只发布已保存版本，编辑框中未保存的内容不会发布。', onOk: () => perform(async () => { await api(`/wiki/pages/${page!.id}/publish`, json({ revision_id: page!.current_revision_id, expected_version: page!.version })); notify('已发布新版本'); }) });
  /** 作用：打开实际 Wiki 修订历史；输入：无；输出：历史窗口。 */
  const openHistory = () => perform(async () => { setHistory((await api<{ items: WikiRevision[] }>(`/wiki/pages/${page!.id}/revisions`)).items); });
  return <div className="knowledge-stack"><Space wrap><Select aria-label="Wiki 页面" style={{ width: 320 }} value={selected ?? ''} options={[{ value: '', label: '新建人工草稿' }, ...pages.map(p => ({ value: p.id, label: `${p.revision.payload.title} · v${p.version}` }))]} onChange={id => {
    setSelected(id || null); setInitialized(true); setEditing(!id); setPublished(null); const p = pages.find(p => p.id === id); setSlug(p?.slug ?? ''); setEditorVersion(p?.version ?? 0); setDraft(p ? { title: p.revision.payload.title, content: p.revision.payload.content, citations: p.revision.payload.citations, input_revision_ids: p.revision.payload.input_revision_ids } : { title: '', content: '', citations: [], input_revision_ids: [] });
  }} /><Tag>{page ? stateLabel(page.revision.payload.state) : '新草稿'}</Tag><Button onClick={() => { setSelected(null); setInitialized(true); setEditing(true); setSlug(''); setEditorVersion(0); setDraft({ title: '', content: '', citations: [], input_revision_ids: [] }); }}>新建 Wiki</Button></Space>
    {page && <><div className="wiki-reading-header"><div><p className="eyebrow">知识导航</p><h2>{page.revision.payload.title}</h2><p>先看资料、引用主题和指标之间的关系，再打开原文核对。</p></div><Space wrap><Button onClick={() => setEditing(v => !v)}>{editing ? '收起编辑器' : '编辑草稿'}</Button><Button disabled={page.revision.payload.state !== 'draft'} onClick={requestPublish}>审核并发布</Button><Button onClick={() => void openHistory()}>版本历史与回滚</Button></Space></div>
      <Tabs items={[
        { key: 'graph', label: '关系图', children: <KnowledgeGraph relatedVersion={metricVersion} pageId={page.id} revisionId={page.current_revision_id} inspect={id => void inspect(id)} openDocument={(doc, rev) => void openDocument(doc, rev)} selectMetric={selectMetric} /> },
        { key: 'reading', label: '阅读解释', children: <div className="wiki-reader"><ReadableMarkdown text={page.revision.payload.content} citations={page.revision.payload.citations} onCitation={id => void inspect(id)} /><div className="wiki-reference-list"><h3>参考依据</h3>{page.revision.payload.citations.map((c, i) => <Button key={c.chunk_id} onClick={() => void inspect(c.chunk_id)}>引用 {i + 1} · 查看原文</Button>)}</div></div> },
      ]} /></>}
    <details className="wiki-editor" open={editing || !page} onToggle={e => setEditing(e.currentTarget.open)}><summary>编辑与生成草稿</summary><div className="knowledge-stack">
    <div className="knowledge-form"><label>页面标识（英文与连字符）<Input aria-label="Wiki 页面标识" disabled={!!page} value={slug} onChange={e => setSlug(e.target.value)} /></label><label>标题<Input aria-label="Wiki 标题" value={draft.title} onChange={e => setDraft(d => ({ ...d, title: e.target.value }))} /></label></div>
    <label>生成草稿的输入文档<Select aria-label="Wiki 输入文档" mode="multiple" style={{ width: '100%' }} value={inputs} onChange={setInputs} options={documents.map(d => ({ value: d.current_revision_id, label: `${d.title} v${d.version}` }))} /></label>
    <Button disabled={!slug || !inputs.length} loading={busy} onClick={() => void perform(async () => { const job = await api<Job>('/wiki/generate', json({ slug, input_revision_ids: inputs, expected_version: page?.version ?? 0 })); notify(`生成草稿已排队：${job.id}；完成后请选择或刷新页面，人工修改不会被覆盖。`); })}>根据所选资料生成新草稿</Button>
    <Alert type="info" message="生成内容需要人工审核；每个正文段落都应保留 [chunk_…] 引用标记。可从原文或检索结果选择引用。" />
    <Button disabled={!references.length} onClick={() => setDraft(d => {
      const fresh = references.filter(r => !d.citations.some(c => c.chunk_id === r.id));
      return { ...d, content: d.content + fresh.map(r => `\n\n${r.text} [${r.id}]`).join(''), citations: [...d.citations, ...fresh.map(r => ({ chunk_id: r.id, excerpt: r.text, text_hash: r.text_hash }))], input_revision_ids: [...new Set([...d.input_revision_ids, ...fresh.map(r => r.revision_id)])] };
    })}>插入所选原文及引用（{references.length}）</Button>
    <label>Wiki 正文<Input.TextArea aria-label="Wiki 正文" rows={12} value={draft.content} onChange={e => setDraft(d => ({ ...d, content: e.target.value }))} /></label>
    <Space wrap><Button type="primary" loading={busy} disabled={!slug || !draft.title || !draft.content || !draft.citations.length} onClick={() => void perform(async () => {
      const result = await api<{ page_id: string; version: number }>(page ? `/wiki/pages/${page.id}` : `/wiki/pages?slug=${encodeURIComponent(slug)}`, json({ title: draft.title, content: draft.content, citations: draft.citations, input_revision_ids: draft.input_revision_ids, expected_version: editorVersion }, page ? 'PUT' : 'POST')); setSelected(result.page_id); setEditorVersion(result.version); notify('已保存为新草稿版本');
    })}>保存人工草稿</Button>
      <Button disabled={!page} onClick={() => { const p = page!; setDraft({ title: p.revision.payload.title, content: p.revision.payload.content, citations: p.revision.payload.citations, input_revision_ids: p.revision.payload.input_revision_ids }); setEditorVersion(p.version); }}>载入最新已保存正文</Button>
      <Button disabled={!page || !page.published_revision_id} onClick={() => void perform(async () => { setPublished(await api<WikiRevision>(`/wiki/pages/${page!.id}/revisions/${page!.published_revision_id}`)); })}>查看已发布内容作对照</Button>
      </Space>
    {published && <article className="knowledge-hit"><h3>已发布 v{published.version} · 与上方草稿对照</h3><ReadableMarkdown text={published.payload.content} citations={published.payload.citations} onCitation={id => void inspect(id)} /></article>}
    <Space wrap>{draft.citations.map((c, i) => <Button key={c.chunk_id} size="small" onClick={() => void inspect(c.chunk_id)}>引用 {i + 1} · 查看原文</Button>)}</Space>
    </div></details>{modalContext}
    <Modal title="Wiki 历史（回滚会创建新草稿）" open={history.length > 0} onCancel={() => setHistory([])} footer={null} width={900}>{history.map(r => <article className="knowledge-hit" key={r.id}><Space><strong>v{r.version} · {stateLabel(r.payload.state)}</strong><Button onClick={() => void perform(async () => { await api(`/wiki/pages/${page!.id}/revert`, json({ revision_id: r.id, expected_version: page!.version })); setHistory([]); notify('已从历史创建新的回滚草稿，尚未发布'); })}>以此版本新建草稿</Button></Space><ReadableMarkdown text={r.payload.content} citations={r.payload.citations} onCitation={id => void inspect(id)} /></article>)}</Modal>
  </div>;
}

/** 作用：编辑和审核结构化口径，展示历史与适用期；输入：指标和文档目录；输出：指标面板。 */
function MetricsPanel({ metrics, documents, busy, perform, snapshot, asOf, notify, initialKey, openSource }: Actions & { metrics: MetricItem[]; documents: DocumentItem[]; snapshot: Snapshot | null; asOf: string; initialKey: { key: string; version: number }; openSource: (id: string) => void }) {
  const [modal, modalContext] = Modal.useModal();
  const [key, setKey] = useState('');
  const [editorVersion, setEditorVersion] = useState(0);
  const [draft, setDraft] = useState<MetricDefinition | null>(null);
  const [history, setHistory] = useState<MetricRevision[]>([]);
  const [editing, setEditing] = useState(false);
  const [consumedFocus, setConsumedFocus] = useState('');
  const metric = metrics.find(m => m.key === key);
  useEffect(() => {
    const focus = `${initialKey.key}:${initialKey.version}`;
    const target = initialKey.key && consumedFocus !== focus ? metrics.find(m => m.key === initialKey.key) : !draft ? metrics[0] : null;
    if (target) { setKey(target.key); setEditorVersion(target.version); setDraft({ ...target.revision.definition }); setEditing(false); setConsumedFocus(focus); }
  }, [metrics, initialKey, consumedFocus, draft]);
  /** 作用：更新单个指标字段；输入：字段名与值；输出：编辑状态。 */
  const update = <K extends keyof MetricDefinition>(field: K, value: MetricDefinition[K]) => setDraft(d => d ? { ...d, [field]: value } : d);
  return <div className="knowledge-stack"><Space wrap><Select aria-label="指标选择" style={{ width: 330 }} value={key || undefined} placeholder="选择指标" options={metrics.map(m => ({ value: m.key, label: `${m.revision.definition.name} · v${m.version} · ${stateLabel(m.revision.definition.review_state)}` }))} onChange={v => { const selected = metrics.find(m => m.key === v)!; setKey(v); setEditorVersion(selected.version); setDraft({ ...selected.revision.definition }); setEditing(false); }} />
    <Button onClick={() => { setKey(''); setEditorVersion(0); setEditing(true); setDraft({ key: '', version: 1, name: '', authority: 'project_policy', review_state: 'draft', unit: 'percent', grain: '', numerator_description: '', denominator_description: '', population_filters: [], required_columns: [], null_policy: '', effective_from: '2026-01-01', effective_to: '2026-02-01', source_revision_ids: [], template_key: null }); }}>新建指标草稿</Button></Space>
    {modalContext}{!draft ? <Empty description="选择一个指标查看分子、分母、来源和版本" /> : <><MetricSummary definition={metric?.revision.definition ?? draft} documents={documents} openSource={openSource} /><Button disabled={!metric} onClick={() => setEditing(v => !v)}>{editing ? '收起规则编辑' : '编辑规则'}</Button><details className="metric-editor" open={editing} onToggle={e => setEditing(e.currentTarget.open)}><summary>修改指标草稿</summary><div className="knowledge-form">
      <label>指标键<Input aria-label="指标键" disabled={!!metric} value={draft.key} onChange={e => update('key', e.target.value)} /></label>
      <label>名称<Input aria-label="指标名称" value={draft.name} onChange={e => update('name', e.target.value)} /></label>
      <label>单位<Select aria-label="指标单位" value={draft.unit} onChange={v => update('unit', v)} options={[{ value: 'count', label: '数量' }, { value: 'percent', label: '百分比（0—100）' }, { value: 'minutes', label: '分钟' }]} /></label>
      <label>分组粒度<Input aria-label="指标粒度" value={draft.grain} onChange={e => update('grain', e.target.value)} /></label>
      <label>分子描述<Input.TextArea aria-label="指标分子" value={draft.numerator_description} onChange={e => update('numerator_description', e.target.value)} /></label>
      <label>分母描述<Input.TextArea aria-label="指标分母" value={draft.denominator_description ?? ''} onChange={e => update('denominator_description', e.target.value || null)} /></label>
      <label>过滤条件（每行一项）<Input.TextArea aria-label="指标过滤" value={draft.population_filters.join('\n')} onChange={e => update('population_filters', e.target.value.split('\n'))} /></label>
      <label>所需字段（每行一项）<Input.TextArea aria-label="指标所需字段" value={draft.required_columns.join('\n')} onChange={e => update('required_columns', e.target.value.split('\n'))} /></label>
      <label>空值及零分母处理<Input.TextArea aria-label="指标空值规则" value={draft.null_policy} onChange={e => update('null_policy', e.target.value)} /></label>
      <label>适用起始日期<Input aria-label="指标生效日期" type="date" value={draft.effective_from} onChange={e => update('effective_from', e.target.value)} /></label>
      <label>截止日期（不含）<Input aria-label="指标截止日期" type="date" value={draft.effective_to ?? ''} onChange={e => update('effective_to', e.target.value || null)} /></label>
      <label>来源文档固定版本<Select aria-label="指标来源" mode="multiple" value={draft.source_revision_ids} onChange={v => update('source_revision_ids', v)} options={[...documents.map(d => ({ value: d.current_revision_id, label: `${d.title} v${d.version}` })), ...draft.source_revision_ids.filter(id => !documents.some(d => d.current_revision_id === id)).map(id => ({ value: id, label: `固定旧版本 ${id}` }))]} /></label>
    </div><Space wrap><Button type="primary" loading={busy} onClick={() => void perform(async () => { const result = await api<{ definition: MetricDefinition }>(metric ? `/metrics/${metric.key}/revisions` : '/metrics', json({ expected_version: editorVersion, definition: { ...draft, version: editorVersion + 1, review_state: 'draft', population_filters: draft.population_filters.filter(Boolean), required_columns: draft.required_columns.filter(Boolean) } })); setKey(result.definition.key); setDraft(result.definition); setEditorVersion(result.definition.version); notify('指标已保存为待审核新版本'); })}>保存指标草稿</Button>
      <Button disabled={!metric} onClick={() => { setDraft({ ...metric!.revision.definition }); setEditorVersion(metric!.version); }}>载入最新已保存指标</Button>
      </Space></details><Space wrap>
      <Button disabled={!metric || metric.revision.definition.review_state !== 'draft'} onClick={() => modal.confirm({ title: '审核当前已保存的指标口径？', okText: '确认审核', cancelText: '取消', content: '请核对分母、过滤、缺失处理、适用期和来源。此操作审核服务器上的已保存版本，编辑框未保存的内容不会生效。', onOk: () => perform(async () => { await api(`/metrics/${metric!.key}/approve`, json({ revision_id: metric!.current_revision_id, expected_version: metric!.version })); notify('已创建已审核指标修订；更新知识快照后供后续分析使用'); }) })}>审核此指标</Button>
      <Button disabled={!metric} onClick={() => void perform(async () => { setHistory((await api<{ items: MetricRevision[] }>(`/metrics/${metric!.key}/revisions`)).items); })}>指标版本历史</Button>
      <Button disabled={!metric || !snapshot || !asOf} onClick={() => void perform(async () => { const result = await api<MetricRevision>(`/metrics/${key}/resolve?snapshot_id=${snapshot!.id}&as_of=${asOf}`); notify(`当前快照与日期适用：${result.definition.name} v${result.version}`); })}>检查所选日期适用性</Button></Space></>}
    <Modal title="固定指标历史" open={history.length > 0} onCancel={() => setHistory([])} footer={null} width={800}>{history.map(r => <article key={r.id} className="knowledge-hit"><h3>v{r.version} · {stateLabel(r.definition.review_state)}</h3><p>分子：{r.definition.numerator_description}</p><p>分母：{r.definition.denominator_description ?? '不适用'}</p><p>过滤：{r.definition.population_filters.join('；')}</p><p>空值：{r.definition.null_policy}</p><p>适用期：{r.definition.effective_from} 至 {r.definition.effective_to ?? '未声明截止'}</p></article>)}</Modal>
  </div>;
}
