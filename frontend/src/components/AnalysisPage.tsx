import { lazy, Suspense, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Alert, Button, Input, Modal, Select, Space, Table } from 'antd';
import type { components } from '../api/schema';
import { analysisApi, eventTypes, jsonRequest, mergeEvents, terminal } from '../api/analysis';
import type { Conversation, Run, RunEvent } from '../api/analysis';
import { ConversationItem } from './ConversationItem';
import { ExecutionTrace } from './ExecutionTrace';
import { runFailureDescription } from './executionPresentation';
import { InvestigationPanel } from './InvestigationPanel';
import type { Investigation } from './InvestigationPanel';

type Source = components['schemas']['SourceDTO'];
type Snapshot = components['schemas']['SnapshotDTO'];
type Artifact = components['schemas']['ArtifactPage'];
const ReportPreview = lazy(() => import("./ReportsPage").then(module => ({ default: module.ReportPreview })));
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
  const [sidebarSlot, setSidebarSlot] = useState<HTMLElement | null>(null);
  const [sessionFilter, setSessionFilter] = useState('');
  useEffect(() => { setSidebarSlot(document.getElementById('workspace-sessions')); }, []);
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
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [creatingInvestigation, setCreatingInvestigation] = useState(false);
  const [goal, setGoal] = useState('');
  const [scope, setScope] = useState('');
  const [branch, setBranch] = useState(false);
  const active = !!run && !terminal(run.status);
  const investigationActive = !!investigation?.active_run_id;

  // 从服务端恢复调查；工具进度到达时同步已落盘的发现，页面断开不影响运行。
  useEffect(() => {
    if (!conversationId) { setInvestigations([]); setInvestigation(null); return; }
    let alive = true;
    void analysisApi<{ items: Investigation[] }>(`/investigations?conversation_id=${conversationId}`).then(result => {
      if (!alive) return;
      setInvestigations(result.items);
      setInvestigation(current => result.items.find(item => item.id === (run?.investigation_id || current?.id)) ?? result.items.at(-1) ?? null);
    }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [conversationId, run?.id, run?.status, events.length]);

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
    setInvestigation(null); setInvestigations([]);
    return created.id;
  }

  /** 作用：排队真实分析；输入：表单条件；输出：新运行状态。 */
  async function submit() {
    setBusy(true); setError('');
    try {
      const id = conversationId || await newConversation();
      let current = investigation;
      if (!current) {
        current = await analysisApi<Investigation>('/investigations', jsonRequest({ conversation_id: id, goal: question.trim().slice(0, 1200), scope: '在所选固定数据与知识版本内核查用户目标；重要歧义需澄清', snapshot_id: snapshotId }));
        setInvestigation(current); setInvestigations(items => [...items, current!]);
      }
      const created = await analysisApi<Run>('/runs', jsonRequest({ conversation_id: id, question: question.trim(), snapshot_id: current.snapshot_id, investigation_id: current.id, expected_investigation_version: current.version }));
      setRun(created); setRuns(current => [...current, created]); setQuestion('');
      setConversations(current => current.map(item => item.id === id ? { ...item, title: created.question.replace(/\s+/g, ' ').trim() } : item));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  /** 作用：显式创建调查或版本分支；输入：目标、范围与所选快照；输出：固定调查。 */
  async function createInvestigation() {
    setBusy(true); setError('');
    try {
      const id = conversationId || await newConversation();
      const created = await analysisApi<Investigation>('/investigations', jsonRequest({ conversation_id: id, goal: goal.trim(), scope: scope.trim(), snapshot_id: snapshotId, parent_id: branch ? investigation?.id : undefined }));
      setInvestigations(items => [...items, created]); setInvestigation(created); setRun(null); setEvents([]); setCreatingInvestigation(false); setQuestion('');
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
    {sidebarSlot && createPortal(<section className="analysis-history" aria-label="分析会话">
      <Button className="new-conversation" block disabled={active || busy} onClick={() => { void newConversation().catch(e => setError(e.message)); }}><span aria-hidden="true">＋</span> 新会话</Button>
      <div className="session-section-title"><h2>最近会话</h2><span>{conversations.length}</span></div>
      {conversations.length > 8 && <Input aria-label="搜索会话" placeholder="搜索会话" value={sessionFilter} onChange={event => setSessionFilter(event.target.value)} allowClear />}
      <div className="session-list">{conversations.filter(item => item.title.toLowerCase().includes(sessionFilter.toLowerCase())).map(item => <ConversationItem title={item.title} selected={conversationId === item.id} disabled={active || busy} key={item.id} onClick={() => setConversationId(item.id)} />)}
        {!conversations.length && <p className="sidebar-empty">开始一次分析，<br />会话会保存在这里。</p>}
      </div>
    </section>, sidebarSlot)}
    <section className={`analysis-workspace${!run ? ' is-empty' : ''}`}>
      <header className="workspace-toolbar"><div className="workspace-toolbar-title"><span className="eyebrow">ANALYSIS</span><span>数据调查</span></div><Button type="text" onClick={() => setDrawer(true)}>查看数据源 <span aria-hidden="true">↗</span></Button></header>
      {error && <Alert className="analysis-alert" type="error" showIcon message={error} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
      {!configured && <Alert className="analysis-alert" type="warning" message="模型未配置或分析服务尚未就绪" description="请检查本机模型配置和后端状态。健康检查不会请求模型。" />}
      {!snapshots.length && <Alert className="analysis-alert" type="info" message="暂无可分析的数据，请先导入数据快照" />}
      <div className="analysis-main">
        <div className="analysis-thread">
          {!run && <div className="welcome-state"><span className="welcome-symbol" aria-hidden="true">✳</span><p className="eyebrow">从数据，到有依据的发现</p><h1>今天，想了解什么？</h1><p>提出一个问题，一起查阅数据、核对线索，<br />让每个结论都能找到出处。</p></div>}
          <div className="analysis-data-bar"><span className="data-bar-icon" aria-hidden="true">▦</span><label htmlFor="snapshot">分析数据</label><Select id="snapshot" aria-label="本次数据快照" value={investigation?.snapshot_id || snapshotId || undefined} disabled={active || busy || !!investigation} onChange={setSnapshotId} options={snapshots.map(item => ({ value: item.id, label: `${item.manifest?.source_kind === 'synthetic' ? '合成测试数据' : 'BTS Reporting'} · ${item.period} · ${item.row_count.toLocaleString()} 条` }))} /></div>
          <div className="investigation-controls"><Select aria-label="切换调查" value={investigation?.id} placeholder="首次提问会建立调查" disabled={active || busy || investigationActive} onChange={id => { const selected = investigations.find(item => item.id === id)!; setInvestigation(selected); setSnapshotId(selected.snapshot_id); setRun(runs.filter(item => item.investigation_id === id).at(-1) ?? null); setEvents([]); }} options={investigations.map(item => ({ value: item.id, label: item.goal }))} /><Button type="text" disabled={active || busy || investigationActive} onClick={() => { setGoal(''); setScope(''); setBranch(false); setCreatingInvestigation(true); }}>新调查</Button>{investigation && <Button type="text" disabled={active || busy || investigationActive} onClick={() => { setGoal(investigation.goal); setScope(investigation.scope); setSnapshotId(investigation.snapshot_id); setBranch(true); setCreatingInvestigation(true); }}>新建分支</Button>}</div>
          {runs.length > 0 && <Select aria-label="历史分析运行" className="run-history-select" value={run?.id} disabled={active || busy} onChange={id => setRun(runs.find(item => item.id === id) ?? null)} options={runs.map(item => ({ value: item.id, label: `${item.question} · ${statusText[item.status]}` }))} />}
        {run && <div className="run-result">
          <div className="analysis-question"><span>你的问题</span><h3>{run.question}</h3></div>
          <ExecutionTrace run={run} events={events} connection={connection} />
          {run.error_code && <Alert type={run.status === 'cancelled' ? 'info' : 'error'} message={run.status === 'cancelled' ? '本次分析已取消' : run.status === 'interrupted' ? '分析因服务中断而停止' : '本次分析未完成'} description={runFailureDescription(run)} />}
          {run.status === 'completed' && run.result?.kind === 'report' && <Suspense fallback={<p>正在加载报告…</p>}><ReportPreview reportId={run.result.report_id} revisionId={run.result.revision_id} /></Suspense>}
          {run.result?.kind === 'clarification' && <Alert type="info" message={run.result.question} description={run.result.missing_requirements.join('；')} />}
          {run.result?.kind === 'query_result' && <p className="analysis-result-note">以下为本次冻结结果；执行与引用有效，业务语义未自动验证。</p>}
          {artifacts.filter(page => page.artifact.run_id === run.id).map(page => <div key={page.artifact.id} className="artifact-preview"><h4>查询结果 · {page.artifact.returned_rows} 行</h4>{page.artifact.truncated && <Alert type="warning" message="结果已截断，不能作为完整统计" />}<Table size="small" pagination={false} scroll={{ x: true }} rowKey="_row" dataSource={page.rows.map((row, index) => Object.fromEntries([['_row', String(index)], ...row.map((cell, col) => [String(col), cell])]))} columns={page.artifact.columns.map((column, index) => ({ title: column.name, dataIndex: String(index), render: displayCell }))} /><p>当前显示 {page.rows.length} 行，起始位置 {page.offset + (page.rows.length ? 1 : 0)}</p>{(page.offset > 0 || page.next_offset !== null) && <Space><Button size="small" disabled={page.offset === 0} onClick={() => { void artifactPage(page, Math.max(0, page.offset - 20)); }}>上一页</Button><Button size="small" disabled={page.next_offset === null} onClick={() => { if (page.next_offset !== null) void artifactPage(page, page.next_offset); }}>下一页</Button></Space>}</div>)}
        </div>}

          {investigation && <InvestigationPanel investigation={investigation} disabled={active || busy || investigationActive} onFollowUp={setQuestion} onError={setError} />}
        </div>
        <div className="composer"><label htmlFor="question">分析问题{investigation ? ' · 继续调查' : ''}</label><Input.TextArea aria-label="分析问题" id="question" rows={2} maxLength={4000} value={question} onChange={e => setQuestion(e.target.value)} placeholder="描述你想了解的问题，或继续追问…" disabled={active || busy || investigationActive} /><div className="composer-bottom"><span>{investigation?.status === 'budget_exhausted' ? '累计运行额度已分配完，历史发现仍可查看' : '关闭页面后运行会继续'}</span><Space>{active && <Button danger onClick={() => { void cancel(); }} disabled={run?.cancel_requested}>停止分析</Button>}<Button type="primary" loading={busy} disabled={active || investigationActive || investigation?.status === 'budget_exhausted' || !configured || !snapshotId || !question.trim()} onClick={() => { void submit(); }}>发送问题</Button></Space></div></div>

        {!run && <div className="starter-prompts" aria-label="问题示例">{['这份数据覆盖哪些日期？', '看看各日期的记录分布', '哪些地方值得进一步核查？'].map(prompt => <button key={prompt} disabled={active || busy || investigationActive} onClick={() => setQuestion(prompt)}>{prompt}<span aria-hidden="true">↗</span></button>)}</div>}
        <p className="workspace-disclaimer">分析可能存在局限，请结合引用依据复核。数据与知识版本在调查创建时固定。</p>
      </div>
    </section>
    <Suspense fallback={<p>正在加载数据源…</p>}>{drawer && <DataSourceDrawer open={drawer} onClose={() => setDrawer(false)} />}</Suspense>
    <Modal title={branch ? '新建调查分支' : '新建调查'} open={creatingInvestigation} onCancel={() => setCreatingInvestigation(false)} onOk={() => { void createInvestigation(); }} confirmLoading={busy} okText="固定范围并创建" okButtonProps={{ disabled: !goal.trim() || !scope.trim() || !snapshotId }}><div className="investigation-form"><label>调查目标<Input.TextArea aria-label="调查目标" value={goal} maxLength={1200} onChange={e => setGoal(e.target.value)} rows={2} /></label><label>范围与已确认条件<Input.TextArea aria-label="调查范围" value={scope} maxLength={1200} onChange={e => setScope(e.target.value)} rows={3} /></label><label>数据快照<Select aria-label="新调查数据快照" value={snapshotId} onChange={setSnapshotId} options={snapshots.map(item => ({ value: item.id, label: `${item.period} · ${item.row_count.toLocaleString()} 条` }))} /></label><p>创建时固定当前知识版本。分支记录来源调查，证据需在新调查中重新核查；不会自动继承旧结论或重置原调查额度。</p></div></Modal>
  </>;
}
