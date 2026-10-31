import { lazy, Suspense, useEffect, useState } from 'react';
import { Alert, Button, Empty, Input, Select, Space, Table } from 'antd';
import type { components } from '../api/schema';
import { analysisApi, eventTypes, jsonRequest, mergeEvents, terminal } from '../api/analysis';
import type { Conversation, Run, RunEvent } from '../api/analysis';
import { ConversationItem } from './ConversationItem';
import { ExecutionTrace } from './ExecutionTrace';

type Source = components['schemas']['SourceDTO'];
type Snapshot = components['schemas']['SnapshotDTO'];
type Artifact = components['schemas']['ArtifactPage'];
const DataSourceDrawer = lazy(() => import('./DataSourceDrawer').then(module => ({ default: module.DataSourceDrawer })));
const statusText: Record<Run['status'], string> = { queued: '已排队', running: '分析中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '服务中断' };

/** 作用：显示类型化冻结单元格；输入：原始值；输出：可读文本，不计算指标。 */
function displayCell(value: unknown): string {
  if (value === null) return '无可用值';
  if (value && typeof value === 'object' && 'type' in value && 'value' in value
      && ['date', 'datetime', 'time', 'decimal', 'integer'].includes(String(value.type))) return String(value.value);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

/** 作用：展示真实分析、会话和执行事件；输入：无；输出：分析页。 */
export function AnalysisPage() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState(() => localStorage.getItem('analysis-conversation') ?? '');
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [snapshotId, setSnapshotId] = useState('');
  const [question, setQuestion] = useState('');
  const [error, setError] = useState('');
  const [connection, setConnection] = useState('');
  const [configured, setConfigured] = useState(false);
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const active = !!run && !terminal(run.status);

  useEffect(() => {
    let alive = true;
    setError('');
    void Promise.all([
      analysisApi<{ items: Conversation[] }>('/conversations'),
      analysisApi<{ items: Source[] }>('/sources'),
      analysisApi<components['schemas']['HealthResponse']>('/health'),
    ]).then(async ([sessions, sources, health]) => {
      const lists = await Promise.all(sources.items.map(source => analysisApi<{ items: Snapshot[] }>(`/sources/${source.id}/snapshots`)));
      if (!alive) return;
      setConversations(sessions.items);
      const available = lists.flatMap(list => list.items);
      setSnapshots(available);
      setSnapshotId(current => current || sources.items.find(source => source.source_kind === 'official_bts')?.current_snapshot_id || sources.items[0]?.current_snapshot_id || available[0]?.id || '');
      setConfigured(health.llm === 'configured_unverified' && health.analysis === 'ready');
    }).catch(e => { if (alive) setError(String(e.message)); });
    return () => { alive = false; };
  }, [attempt]);

  useEffect(() => {
    if (!conversationId) { setRuns([]); setRun(null); setEvents([]); return; }
    localStorage.setItem('analysis-conversation', conversationId);
    let alive = true;
    void analysisApi<{ items: Run[] }>(`/conversations/${conversationId}`).then(result => {
      if (!alive) return;
      const sorted = [...result.items].sort((a, b) => a.created_at.localeCompare(b.created_at));
      setRuns(current => [...sorted, ...current.filter(item => item.conversation_id === conversationId && !sorted.some(old => old.id === item.id))]);
      setRun(current => current?.conversation_id === conversationId ? current : sorted.at(-1) ?? null);
      setEvents([]);
    }).catch(e => { if (alive) { setError(e.message); setConversationId(''); localStorage.removeItem('analysis-conversation'); } });
    return () => { alive = false; };
  }, [conversationId]);

  useEffect(() => {
    if (!run?.id) return;
    const id = run.id;
    setEvents([]); setConnection('正在连接进度…'); setArtifacts([]);
    const stream = new EventSource(`/api/v1/runs/${id}/events`);
    /** 作用：恢复服务端运行状态；输入：无；输出：更新当前记录。 */
    const refresh = () => void analysisApi<Run>(`/runs/${id}`).then(updated => {
      setRun(current => current?.id === id ? updated : current);
      setRuns(current => current.map(item => item.id === id ? updated : item));
    }).catch(e => setError(e.message));
    /** 作用：合并真实事件并在终态停止订阅；输入：SSE 帧；输出：更新状态。 */
    const receive = (message: MessageEvent) => {
      try {
        const event = JSON.parse(message.data) as RunEvent;
        if (event.run_id !== id || !Number.isInteger(event.seq) || !eventTypes.includes(event.type)) throw new Error('进度响应不符合契约');
        setEvents(current => mergeEvents(current, event));
        if (['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.type)) {
          stream.close(); setConnection(''); refresh();
          void analysisApi<{ items: Conversation[] }>('/conversations').then(value => setConversations(value.items)).catch(e => setError(e.message));
        }
      } catch { setError('进度响应不符合契约，请刷新运行状态'); }
    };
    eventTypes.forEach(type => stream.addEventListener(type, receive as EventListener));
    stream.onopen = () => { setConnection('进度已连接'); refresh(); };
    stream.onerror = () => { setConnection('进度连接中断，正在自动重连；后端运行会继续'); refresh(); };
    return () => stream.close();
  }, [run?.id]);

  useEffect(() => {
    if (!run || run.status !== 'completed' || run.result?.kind !== 'query_result') return;
    let alive = true;
    void Promise.all(run.result.artifact_ids.map(id => analysisApi<Artifact>(`/runs/${run.id}/artifacts/${id}`)))
      .then(values => { if (alive) setArtifacts(values); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [run?.id, run?.status]);

  /** 作用：创建新会话；输入：无；输出：会话标识。 */
  async function newConversation() {
    const created = await analysisApi<Conversation>('/conversations', { method: 'POST' });
    setConversations(current => [created, ...current]); setConversationId(created.id);
    return created.id;
  }

  /** 作用：排队真实分析；输入：表单条件；输出：新运行状态。 */
  async function submit() {
    setBusy(true); setError('');
    try {
      const id = conversationId || await newConversation();
      const context = runs.filter(item => item.status === 'completed' && item.snapshot_id === snapshotId).slice(-2).map(item => item.id);
      const created = await analysisApi<Run>('/runs', jsonRequest({ conversation_id: id, question: question.trim(), snapshot_id: snapshotId, context_run_ids: context }));
      setRun(created); setRuns(current => [...current, created]); setQuestion('');
      setConversations(current => current.map(item => item.id === id ? { ...item, title: created.question.replace(/\s+/g, ' ').trim() } : item));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  /** 作用：分页读取冻结结果；输入：产物及偏移；输出：更新预览。 */
  async function artifactPage(page: Artifact, offset: number) {
    try {
      const updated = await analysisApi<Artifact>(`/runs/${page.artifact.run_id}/artifacts/${page.artifact.id}?offset=${offset}&limit=20`);
      setArtifacts(current => current.map(item => item.artifact.id === updated.artifact.id ? updated : item));
    } catch (e) { setError((e as Error).message); }
  }

  /** 作用：显式停止实际后端执行；输入：当前运行；输出：取消状态。 */
  async function cancel() {
    if (!run) return;
    try { const updated = await analysisApi<Run>(`/runs/${run.id}/cancel`, { method: 'POST' }); setRun(updated); }
    catch (e) { setError((e as Error).message); }
  }

  return <>
    <div className="page-heading"><div><p className="eyebrow">ANALYSIS WORKSPACE</p><h1>从业务问题开始分析</h1><p>查依据、看结构、执行安全查询。当前提供查询结果，完整报告将在阶段 4 开放。</p></div><Button onClick={() => setDrawer(true)}>查看数据源</Button></div>
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
    {!configured && <Alert type="warning" message="模型未配置或分析服务尚未就绪" description="请检查本机模型配置和后端状态。健康检查不会请求模型。" />}
    {!snapshots.length && <Alert type="info" message="暂无可分析的数据，请先导入数据快照" />}
    <div className="mobile-conversation"><Select aria-label="切换分析会话" value={conversationId || undefined} placeholder="选择会话" disabled={active || busy} onChange={setConversationId} options={conversations.map(item => ({ value: item.id, label: item.title }))} /><Button disabled={active || busy} onClick={() => { void newConversation().catch(e => setError(e.message)); }}>新会话</Button></div>
    <div className="analysis-grid analysis-conversation-layout">
      <aside className="panel analysis-history"><Space direction="vertical"><h2>分析会话</h2><Button disabled={active || busy} onClick={() => { void newConversation().catch(e => setError(e.message)); }}>新会话</Button></Space>
        {conversations.map(item => <ConversationItem title={item.title} selected={conversationId === item.id} disabled={active || busy} key={item.id} onClick={() => setConversationId(item.id)} />)}
      </aside>
      <section className="panel question-panel analysis-main">
        <label htmlFor="snapshot">本次数据快照</label>
        <Select id="snapshot" aria-label="本次数据快照" value={snapshotId || undefined} disabled={active || busy} style={{ width: '100%' }} onChange={setSnapshotId} options={snapshots.map(item => ({ value: item.id, label: `${item.manifest?.source ?? '航班数据'} · ${item.period} · ${item.row_count.toLocaleString()} 条${item.manifest?.source_kind === 'synthetic' ? '（合成）' : ''}` }))} />
        <p className="analysis-hint">涉及项目指标时需要人工审核的规则；草稿不会自动生效。切换快照会建立新的分析上下文。</p>
        {runs.length > 0 && <Select aria-label="历史分析运行" value={run?.id} disabled={active || busy} style={{ width: '100%' }} onChange={id => setRun(runs.find(item => item.id === id) ?? null)} options={runs.map(item => ({ value: item.id, label: `${item.question} · ${statusText[item.status]}` }))} />}
        {!run && <Empty description="输入问题后，模型会查阅依据并调用实际工具" />}
        {run && <div className="run-result">
          <div className="analysis-question"><span>你的问题</span><h3>{run.question}</h3></div>
          <ExecutionTrace run={run} events={events} connection={connection} />
          {run.error_code && <Alert type={run.status === 'cancelled' ? 'info' : 'error'} message={run.status === 'cancelled' ? '本次分析已取消' : run.status === 'interrupted' ? '分析因服务中断而停止' : '本次分析未完成'} description="可修改问题后重新发起，具体原因可在执行详情中查看。" />}
          {run.result?.kind === 'clarification' && <Alert type="info" message={run.result.question} description={run.result.missing_requirements.join('；')} />}
          {run.result?.kind === 'query_result' && <p className="analysis-result-note">以下为本次冻结结果；执行与引用有效，业务语义未自动验证。</p>}
          {artifacts.filter(page => page.artifact.run_id === run.id).map(page => <div key={page.artifact.id} className="artifact-preview"><h4>查询结果 · {page.artifact.returned_rows} 行</h4>{page.artifact.truncated && <Alert type="warning" message="结果已截断，不能作为完整统计" />}<Table size="small" pagination={false} scroll={{ x: true }} rowKey="_row" dataSource={page.rows.map((row, index) => Object.fromEntries([['_row', String(index)], ...row.map((cell, col) => [String(col), cell])]))} columns={page.artifact.columns.map((column, index) => ({ title: column.name, dataIndex: String(index), render: displayCell }))} /><p>当前显示 {page.rows.length} 行，起始位置 {page.offset + (page.rows.length ? 1 : 0)}</p>{(page.offset > 0 || page.next_offset !== null) && <Space><Button size="small" disabled={page.offset === 0} onClick={() => { void artifactPage(page, Math.max(0, page.offset - 20)); }}>上一页</Button><Button size="small" disabled={page.next_offset === null} onClick={() => { if (page.next_offset !== null) void artifactPage(page, page.next_offset); }}>下一页</Button></Space>}</div>)}
        </div>}
        <div className="composer"><label htmlFor="question">分析问题</label><Input.TextArea id="question" rows={3} maxLength={4000} value={question} onChange={e => setQuestion(e.target.value)} placeholder="例如：查看本月数据的日期范围和记录数" disabled={active || busy} /><div className="composer-bottom"><span>关闭页面后运行会继续</span><Space>{active && <Button danger onClick={() => { void cancel(); }} disabled={run?.cancel_requested}>停止分析</Button>}<Button type="primary" loading={busy} disabled={active || !configured || !snapshotId || !question.trim()} onClick={() => { void submit(); }}>发送问题</Button></Space></div></div>
      </section>

    </div>
    <Suspense fallback={<p>正在加载数据源…</p>}>{drawer && <DataSourceDrawer open={drawer} onClose={() => setDrawer(false)} />}</Suspense>
  </>;
}
