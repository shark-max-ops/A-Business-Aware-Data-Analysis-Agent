import { SharkMark } from './SharkMark';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Alert, Button, Drawer, Input, Modal, Select } from 'antd';
import type { components } from '../api/schema';
import { analysisApi, jsonRequest, terminal } from '../api/analysis';
import type { Conversation, Run } from '../api/analysis';
import { investigationRuns } from '../api/runHistory';
import { loadHealth } from '../api/client';
import { ConversationItem } from './ConversationItem';
import { RunTurn } from './RunTurn';
import { InvestigationPanel, InvestigationInfo } from './InvestigationPanel';
import type { Investigation } from './InvestigationPanel';
import { dateRangeError, dateRangeLabel, snapshotRange } from '../api/data';
import type { DateRange } from '../api/data';
import { DateRangeFields } from './DateRangeFields';
import { SnapshotSummary } from './SnapshotSummary';
import { AnalysisComposer } from './AnalysisComposer';

type Source = components['schemas']['SourceDTO'];
type Snapshot = components['schemas']['SnapshotDTO'];
const DataSourceDrawer = lazy(() => import('./DataSourceDrawer').then(module => ({ default: module.DataSourceDrawer })));
const statusText: Record<Run['status'], string> = { queued: '已排队', running: '分析中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '服务中断' };

/** 作用：展示真实分析、会话和执行事件；输入：无；输出：分析页。 */
export function AnalysisPage() {
  const [sidebarSlot, setSidebarSlot] = useState<HTMLElement | null>(null);
  const [sessionFilter, setSessionFilter] = useState('');
  useEffect(() => { setSidebarSlot(document.getElementById('workspace-sessions')); }, []);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState(() => localStorage.getItem('analysis-conversation') ?? '');
  const [runs, setRuns] = useState<Run[]>([]);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [snapshotId, setSnapshotId] = useState('');
  const [defaultSnapshotId, setDefaultSnapshotId] = useState('');
  const [dateRange, setDateRange] = useState<DateRange>();
  const [formSnapshotId, setFormSnapshotId] = useState('');
  const [formDateRange, setFormDateRange] = useState<DateRange>();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [investigationError, setInvestigationError] = useState('');
  const [configured, setConfigured] = useState(false);
  const [analysisModel, setAnalysisModel] = useState<string | null>(null);
  const [compactionEnabled, setCompactionEnabled] = useState(false);
  const [compactNotice, setCompactNotice] = useState<{ key: string; runCount: number; text: string } | null>(null);
  const [requestingCompact, setRequestingCompact] = useState(false);
  const [busy, setBusy] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [infoOpen, setInfoOpen] = useState(false);
  const [findingsOpen, setFindingsOpen] = useState(false);
  const [pendingFollowUp, setPendingFollowUp] = useState<string | null>(null);
  const [legacy, setLegacy] = useState(false);
  const [restored, setRestored] = useState('');
  const [activity, setActivity] = useState(0);
  const [shown, setShown] = useState(20);
  const [away, setAway] = useState(false);
  const [locationVersion, setLocationVersion] = useState(0);
  const thread = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const jumpTarget = useRef<{ id: string; report?: boolean } | null>(null);
  const restoredReport = useRef<string | null>(null);
  const selectionVersion = useRef(0);
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [creatingInvestigation, setCreatingInvestigation] = useState(false);
  const [goal, setGoal] = useState('');
  const [scope, setScope] = useState('');
  const [branch, setBranch] = useState(false);
  const run = runs.find(item => item.conversation_id === conversationId && !terminal(item.status));
  const active = !!run;
  const investigationActive = !!investigation?.active_run_id;
  const loading = !!conversationId && restored !== conversationId;
  const blocked = active || busy || investigationActive || loading;
  const visibleRuns = investigationRuns(runs.filter(item => item.conversation_id === conversationId), legacy ? null : investigation?.id ?? null);
  const latestResult = visibleRuns.at(-1)?.result;
  const checkpoint = investigation?.compacted_context;
  const idleUsage = !run && checkpoint?.run_count === visibleRuns.length ? checkpoint?.usage : null;
  const contextUsage = idleUsage ?? (run ?? visibleRuns.at(-1))?.context_usage;
  const percent = contextUsage ? Math.min(100, Math.floor(contextUsage.estimated_input / contextUsage.input_budget * 100)) : null;
  const draftKey = `${conversationId}:${legacy ? 'legacy' : investigation?.id ?? 'new'}`;
  const question = drafts[draftKey] ?? '';
  const exhausted = investigation?.status === 'budget_exhausted';
  const setQuestion = (value: string) => setDrafts(current => ({ ...current, [draftKey]: value }));
  const onActivity = useCallback(() => setActivity(value => value + 1), []);
  const onResultArrive = useCallback((id: string, live: boolean) => {
    const restoredResult = restoredReport.current === id;
    if (restoredResult) restoredReport.current = null;
    if (!follow.current || (!live && !restoredResult)) return;
    follow.current = false;
    jumpTarget.current = { id, report: true };
    setAway(true);
    setLocationVersion(value => value + 1);
  }, []);
  const onUpdate = useCallback((updated: Run) => setRuns(current => current.map(item => item.id === updated.id && updated.seq >= item.seq ? updated : item)), []);
  const onContextUsage = useCallback((id: string, seq: number, usage: NonNullable<Run['context_usage']>) => {
    setRuns(current => current.map(item => item.id === id && seq > item.seq ? { ...item, seq, context_usage: usage } : item));
  }, []);
  const currentCount = investigation ? investigation.findings.filter(item => !investigation.findings.some(next => next.supersedes === item.id)).length : 0;
  const selectedSnapshot = snapshots.find(item => item.id === snapshotId);
  const formSnapshot = snapshots.find(item => item.id === formSnapshotId);
  const rangeError = dateRangeError(selectedSnapshot, dateRange);
  const formRangeError = dateRangeError(formSnapshot, formDateRange);

  // 快照变更时重置候选范围；已创建调查始终展示服务端冻结范围。
  useEffect(() => { setDateRange(snapshotRange(selectedSnapshot)); }, [selectedSnapshot]);

  // 工具进度只更新调查内容，不改变用户选中的调查。
  useEffect(() => {
    if (!conversationId || restored !== conversationId) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      void analysisApi<{ items: Investigation[] }>(`/investigations?conversation_id=${conversationId}`).then(result => {
        if (!alive) return;
        setInvestigationError('');
        setInvestigations(result.items);
        setInvestigation(current => result.items.find(item => item.id === current?.id) ?? current);
      }).catch(e => { if (alive) { setError(e.message); setInvestigationError(e.message); } });
      void analysisApi<{ items: Conversation[] }>('/conversations').then(value => { if (alive) setConversations(value.items); }).catch(e => { if (alive) setError(e.message); });
    }, 200);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [conversationId, restored, run?.id, run?.status, activity]);

  useEffect(() => {
    let alive = true;
    setError('');
    void Promise.all([
      analysisApi<{ items: Conversation[] }>('/conversations'),
      analysisApi<{ items: Source[] }>('/sources'),
      loadHealth(),
    ]).then(async ([sessions, sources, health]) => {
      const lists = await Promise.all(sources.items.map(source => analysisApi<{ items: Snapshot[] }>(`/sources/${source.id}/snapshots`)));
      if (!alive) return;
      setConversations(sessions.items);
      const available = lists.flatMap(list => list.items);
      setSnapshots(available);
      const initial = sources.items.find(source => source.source_kind === 'official_bts')?.current_snapshot_id || sources.items.find(source => source.current_snapshot_id)?.current_snapshot_id || available[0]?.id || '';
      setDefaultSnapshotId(initial);
      setSnapshotId(current => current || initial);
      setConfigured((health.kind === 'ready' || health.kind === 'degraded') && health.health.llm === 'configured_unverified' && health.health.analysis === 'ready');
      setAnalysisModel(health.kind === 'ready' || health.kind === 'degraded' ? health.health.analysis_model ?? null : null);
      setCompactionEnabled((health.kind === 'ready' || health.kind === 'degraded') && health.health.context_compaction === true);
    }).catch(e => { if (alive) setError(String(e.message)); });
    return () => { alive = false; };
  }, [attempt, catalogAttempt]);

  useEffect(() => {
    if (!conversationId) { setRuns([]); setInvestigation(null); setInvestigations([]); setRestored(''); return; }
    localStorage.setItem('analysis-conversation', conversationId);
    let alive = true;
    const version = selectionVersion.current;
    void Promise.all([analysisApi<{ items: Run[] }>(`/conversations/${conversationId}`), analysisApi<{ items: Investigation[] }>(`/investigations?conversation_id=${conversationId}`)]).then(([result, list]) => {
      if (!alive) return;
      const sorted = [...result.items].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
      setInvestigationError('');
      setRuns(current => [...sorted, ...current.filter(item => item.conversation_id === conversationId && !sorted.some(old => old.id === item.id))]);
      const latest = sorted.find(item => !terminal(item.status)) ?? sorted.at(-1);
      restoredReport.current = latest?.result?.kind === 'report' ? latest.id : null;
      const selected = list.items.find(item => item.id === latest?.investigation_id) ?? (!latest ? list.items.at(-1) : null) ?? null;
      setInvestigations(current => [...list.items, ...current.filter(item => item.conversation_id === conversationId && !list.items.some(old => old.id === item.id))]);
      if (version === selectionVersion.current) { setInvestigation(selected); setLegacy(!!latest && !latest.investigation_id); if (selected) setSnapshotId(selected.snapshot_id); }
      setRestored(conversationId);
    }).catch(e => {
      if (!alive) return;
      setError(e.message); setRestored(conversationId);
      setRuns([]); setInvestigation(null); setInvestigations([]); setLegacy(false);
      setConversationId(''); localStorage.removeItem('analysis-conversation');
    });
    return () => { alive = false; };
  }, [conversationId, attempt]);

  useEffect(() => {
    setShown(20); setInfoOpen(false); setFindingsOpen(false); setPendingFollowUp(null); follow.current = true; setAway(false);
  }, [conversationId, investigation?.id, legacy]);
  useLayoutEffect(() => {
    const element = thread.current;
    if (!element) return;
    if (anchor.current) { element.scrollTop = anchor.current.top + element.scrollHeight - anchor.current.height; anchor.current = null; }
    else if (jumpTarget.current) {
      const target = jumpTarget.current;
      jumpTarget.current = null;
      const turn = document.getElementById(`turn-${target.id}`);
      const destination = target.report ? turn?.querySelector('.analysis-report-preview') ?? turn : turn;
      if (destination) element.scrollTop += destination.getBoundingClientRect().top - element.getBoundingClientRect().top - 6;
    }
    else if (follow.current) element.scrollTop = element.scrollHeight;
  }, [locationVersion, visibleRuns.length, shown, activity, conversationId, investigation?.id]);
  useEffect(() => {
    const element = thread.current;
    if (!element) return;
    const observer = new ResizeObserver(() => { if (follow.current) element.scrollTop = element.scrollHeight; });
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, [visibleRuns.length, shown]);

  function selectInvestigation(id: string) {
    selectionVersion.current++;
    const selected = investigations.find(item => item.id === id) ?? null;
    setLegacy(id === 'legacy'); setInvestigation(selected);
    if (selected) setSnapshotId(selected.snapshot_id);
  }
  function locateRun(id: string) {
    const index = visibleRuns.findIndex(item => item.id === id);
    if (index < 0) return;
    follow.current = false; setAway(true); jumpTarget.current = { id };
    setLocationVersion(value => value + 1); setShown(Math.max(shown, visibleRuns.length - index));
  }
  function followUp(value: string) {
    if (question.trim() && question !== value) setPendingFollowUp(value);
    else fillFollowUp(value);
  }
  function fillFollowUp(value: string) {
    setQuestion(value); setFindingsOpen(false); setPendingFollowUp(null);
    window.setTimeout(() => document.getElementById('question')?.focus(), 0);
  }

  /** 作用：创建新会话；输入：无；输出：会话标识。 */
  async function newConversation(initialDraft = '') {
    setBusy(true);
    try {
      const created = await analysisApi<Conversation>('/conversations', { method: 'POST' });
      setDrafts(items => ({ ...items, [`${created.id}:new`]: initialDraft }));
      setConversations(current => [created, ...current]); setConversationId(created.id);
      setInvestigation(null); setInvestigations([]); setLegacy(false); setRuns([]);
      return created.id;
    } finally { setBusy(false); }
  }

  /** 作用：排队真实分析；输入：表单条件；输出：新运行状态。 */
  async function submit() {
    if (!investigation && rangeError) { setError(rangeError); return; }
    setBusy(true); setError('');
    try {
      const id = conversationId || await newConversation(question);
      setBusy(true);
      let current = investigation;
      if (!current) {
        current = await analysisApi<Investigation>('/investigations', jsonRequest({ conversation_id: id, goal: question.trim().slice(0, 1200), scope: '在所选固定数据与知识版本内核查用户目标；重要歧义需澄清', snapshot_id: snapshotId, date_range: dateRange }));
        setDrafts(items => ({ ...items, [`${id}:${current!.id}`]: question }));
        selectionVersion.current++; setInvestigation(current); setInvestigations(items => [...items, current!]);
      }
      const created = await analysisApi<Run>('/runs', jsonRequest({
        conversation_id: id, question: question.trim(), snapshot_id: current.snapshot_id,
        investigation_id: current.id, expected_investigation_version: current.version,
      }));
      follow.current = true; setAway(false); setRuns(items => [...items, created]); setDrafts(items => ({ ...items, [draftKey]: '', [`${id}:${current!.id}`]: '' })); onActivity();
      setConversations(current => current.map(item => item.id === id ? { ...item, title: created.question.replace(/\s+/g, ' ').trim() } : item));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  /** 作用：显式创建调查或版本分支；输入：目标、范围与所选快照；输出：固定调查。 */
  async function createInvestigation() {
    if (formRangeError) { setError(formRangeError); return; }
    setBusy(true); setError('');
    try {
      const id = conversationId || await newConversation();
      setBusy(true);
      const created = await analysisApi<Investigation>('/investigations', jsonRequest({ conversation_id: id, goal: goal.trim(), scope: scope.trim(), snapshot_id: formSnapshotId, date_range: formDateRange, parent_id: branch ? investigation?.id : undefined }));
      selectionVersion.current++; setInvestigations(items => [...items, created]); setInvestigation(created); setSnapshotId(created.snapshot_id); setLegacy(false); setCreatingInvestigation(false);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  /** 作用：隔离新调查草稿与当前固定范围；输入：是否分支；输出：创建弹窗。 */
  function openInvestigation(asBranch: boolean) {
    setBranch(asBranch); setGoal(asBranch ? investigation?.goal ?? '' : ''); setScope(asBranch ? investigation?.scope ?? '' : '');
    const id = asBranch ? investigation?.snapshot_id || snapshotId : investigation ? defaultSnapshotId || snapshotId : snapshotId;
    setFormSnapshotId(id);
    setFormDateRange(asBranch ? investigation?.date_range ?? snapshotRange(snapshots.find(item => item.id === id)) : snapshotRange(snapshots.find(item => item.id === id)));
    setCreatingInvestigation(true);
  }

  /** 立即整理空闲调查，活动运行在当前轮的安全边界执行。 */
  async function requestCompact() {
    const version = selectionVersion.current;
    setRequestingCompact(true);
    setError('');
    try {
      if (!run && investigation) {
        const updated = await analysisApi<Investigation>(`/investigations/${investigation.id}/compact`, { method: 'POST' });
        if (version !== selectionVersion.current) return;
        setInvestigation(current => current?.id === updated.id ? updated : current);
        const saved = updated.compacted_context;
        const reduced = saved && saved.before > saved.usage.estimated_input && !idleUsage;
        setCompactNotice({ key: draftKey, runCount: visibleRuns.length, text: reduced
          ? `续查上下文已压缩 · ${saved.before.toLocaleString()} → ${saved.usage.estimated_input.toLocaleString()}（估算）`
          : '当前续查上下文已足够精简' });
        return;
      }
      if (!run) return;
      await analysisApi<Run>(`/runs/${run.id}/compact`, { method: 'POST' });
      setRuns(items => items.map(item => item.id === run.id ? { ...item, compact_context: true } : item));
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setRequestingCompact(false);
    }
  }

  /** 作用：显式停止实际后端执行；输入：当前运行；输出：取消状态。 */
  async function cancel() {
    if (!run) return;
    try { const updated = await analysisApi<Run>(`/runs/${run.id}/cancel`, { method: 'POST' }); onUpdate(updated); onActivity(); }
    catch (e) { setError((e as Error).message); }
  }

  return <>
    {sidebarSlot && createPortal(<section className="analysis-history" aria-label="分析会话">
      <Button className="new-conversation" block disabled={blocked} onClick={() => { setSnapshotId(defaultSnapshotId); setDateRange(snapshotRange(snapshots.find(item => item.id === defaultSnapshotId))); void newConversation().catch(e => setError(e.message)); }}><span aria-hidden="true">＋</span> 新会话</Button>
      <div className="session-section-title"><h2>最近会话</h2><span>{conversations.length}</span></div>
      {conversations.length > 8 && <Input aria-label="搜索会话" placeholder="搜索会话" value={sessionFilter} onChange={event => setSessionFilter(event.target.value)} allowClear />}
      <div className="session-list">{conversations.filter(item => item.title.toLowerCase().includes(sessionFilter.toLowerCase())).map(item => <ConversationItem title={item.title} selected={conversationId === item.id} disabled={blocked} key={item.id} onClick={() => setConversationId(item.id)} />)}
        {!conversations.length && <p className="sidebar-empty">开始一次分析，<br />会话会保存在这里。</p>}
      </div>
    </section>, sidebarSlot)}
    <section className={`analysis-workspace${!visibleRuns.length ? ' is-empty' : ''}`}>
      <header className="workspace-toolbar"><div className="workspace-toolbar-title"><span className="eyebrow">ANALYSIS</span><span title={investigation?.goal}>{investigation?.goal || '数据调查'}</span></div><div className="workspace-actions"><Button type="text" disabled={!investigation || loading} onClick={() => setInfoOpen(true)}>调查信息</Button><Button type="text" disabled={!investigation || loading} onClick={() => setFindingsOpen(true)}>调查发现 · {currentCount}</Button><Button type="text" onClick={() => setDrawer(true)}>工作区数据源 ↗</Button></div></header>
      {error && <Alert className="analysis-alert" type="error" showIcon message={error} action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} />}
      {!configured && <Alert className="analysis-alert" type="warning" message="模型未配置或分析服务尚未就绪" description="请检查本机模型配置和后端状态。健康检查不会请求模型。" />}
      {!snapshots.length && <Alert className="analysis-alert" type="info" message="暂无可分析的数据，请先导入数据快照" />}
      <div className="analysis-main">
        <div className="analysis-status-bar">
          <div className="analysis-data-bar"><label htmlFor="snapshot">分析数据</label><Select id="snapshot" aria-label="本次数据快照" value={investigation?.snapshot_id || snapshotId || undefined} disabled={blocked || !!investigation || legacy} onChange={setSnapshotId} options={snapshots.map(item => ({ value: item.id, label: `${item.manifest?.source_kind === 'synthetic' ? '合成测试数据' : 'BTS Reporting'} · ${item.period ?? (item.periods ?? []).join('、')} · ${item.row_count.toLocaleString()} 条（完整快照） · ${item.id.slice(-8)}` }))} /></div>
          <div className="investigation-controls"><Select aria-label="切换调查" value={legacy ? 'legacy' : investigation?.id} placeholder="首次提问会建立调查" disabled={blocked} onChange={selectInvestigation} options={[...investigations.map(item => ({ value: item.id, label: item.goal })), ...(runs.some(item => !item.investigation_id) ? [{ value: 'legacy', label: '历史独立运行' }] : [])]} /><Button type="text" disabled={blocked} onClick={() => openInvestigation(false)}>新调查</Button>{investigation && <Button type="text" disabled={blocked} onClick={() => openInvestigation(true)}>新建分支</Button>}</div>
          {investigation ? <p className="fixed-date-range">固定日期范围：{dateRangeLabel(investigation.date_range ?? snapshotRange(snapshots.find(item => item.id === investigation.snapshot_id)))}。修改数据或日期请新建调查或分支。</p> : !legacy && <DateRangeFields snapshot={selectedSnapshot} value={dateRange} onChange={setDateRange} disabled={blocked} />}
          {investigation && <p className="investigation-status">{investigation.status === 'running' ? '核查中' : investigation.status === 'budget_exhausted' ? '累计额度已用完' : '可继续调查'}</p>}
          {visibleRuns.length > 0 && <Select<string> aria-label="定位轮次" className="run-history-select" placeholder="定位到历史问题" value={undefined} onChange={locateRun} options={visibleRuns.map(item => ({ value: item.id, label: `${item.question} · ${statusText[item.status]}` }))} />}
        </div>
        <div className="analysis-thread-frame">
        <div className="analysis-thread" ref={thread} onScroll={() => { const element = thread.current; if (element) { follow.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 80; setAway(!follow.current); } }}><div className="analysis-turns">
          {loading && <p role="status">正在恢复调查与历史…</p>}
          {!loading && !visibleRuns.length && <div className="welcome-state"><SharkMark className="welcome-symbol" /><p className="eyebrow">从数据，到有依据的发现</p><h1>今天，想了解什么？</h1><p>提出一个问题，一起查阅数据、核对线索，<br />让每个结论都能找到出处。</p></div>}
          {visibleRuns.length > shown && <Button block onClick={() => { const element = thread.current; if (element) anchor.current = { height: element.scrollHeight, top: element.scrollTop }; follow.current = false; setShown(count => count + 20); }}>显示更早轮次</Button>}
          {!loading && visibleRuns.slice(-shown).map(item => <RunTurn key={`${conversationId}:${item.id}`} run={item} findingCount={investigation?.findings.filter(f => f.run_id === item.id).length ?? 0} onUpdate={onUpdate} onContextUsage={onContextUsage} onActivity={onActivity} onFindings={() => setFindingsOpen(true)} onResultArrive={onResultArrive} investigation={investigation ?? undefined} snapshot={snapshots.find(snapshot => snapshot.id === item.snapshot_id)} disabled={blocked} onFollowUp={followUp} onLocateRun={locateRun} investigationError={investigationError} />)}
        </div></div>
        {away && <button type="button" className="return-latest" aria-label="回到最新" title="回到最新"
          onClick={() => { follow.current = true; setAway(false); if (thread.current) thread.current.scrollTop = thread.current.scrollHeight; }}>
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M10 4v11m-4.5-4.5L10 15l4.5-4.5" /></svg>
        </button>}
        </div>
        {legacy && <Alert type="info" message="历史独立运行仅供查看，继续分析请选择或新建调查" />}
        <AnalysisComposer key={draftKey} question={question} onQuestion={setQuestion}
          onSend={() => { void submit(); }} onStop={() => { void cancel(); }}
          onCompact={() => { void requestCompact(); }}
          onFindings={() => setFindingsOpen(true)} onData={() => setDrawer(true)}
          model={run?.model || analysisModel}
          continuing={!!investigation} active={active} busy={busy}
          disabled={legacy || loading || busy || (investigationActive && !active)}
          canSend={!blocked && !legacy && !exhausted && configured && !!snapshotId && (!!investigation || !rangeError) && !!question.trim()}
          stopping={!!run?.cancel_requested}
          compactDisabled={!compactionEnabled ? '当前后端未启用上下文压缩'
            : run?.cancel_requested ? '本轮正在停止'
            : !active && !visibleRuns.length ? '当前会话暂无可压缩的历史'
            : requestingCompact ? '正在发送压缩请求'
            : run?.compact_context ? '此轮已请求压缩' : ''}
        />

        <div className="context-meter" role="status" title={percent === null ? '暂无上下文计量' : idleUsage ? '续查起始上下文估算，尚未加入下一问' : '最近一次分析请求占可用输入预算的比例（服务端估算）'}>
          {!active && <span>{requestingCompact ? '正在压缩…' : compactNotice?.key === draftKey && compactNotice.runCount === visibleRuns.length ? compactNotice.text : ''}</span>}
          <span className="context-meter-ring" aria-hidden="true"
            style={{ backgroundImage: `conic-gradient(currentColor ${percent ?? 0}%, var(--line) 0)` }} />
          <span className="sr-only">上下文使用估算</span>
          <span>{percent === null ? '—' : `${percent}%`}</span>
        </div>
      </div>
    </section>
    <Suspense fallback={<p>正在加载数据源…</p>}>{drawer && <DataSourceDrawer open={drawer} onClose={() => setDrawer(false)} onActivated={() => { if (!investigation) setSnapshotId(''); setCatalogAttempt(value => value + 1); }} />}</Suspense>
    <Drawer title="调查信息" open={infoOpen} onClose={() => setInfoOpen(false)} width="min(560px, 100vw)">{investigation && <InvestigationInfo investigation={investigation} />}</Drawer>
    <Modal title="替换当前草稿？" open={pendingFollowUp !== null} onCancel={() => setPendingFollowUp(null)} onOk={() => { if (pendingFollowUp !== null) fillFollowUp(pendingFollowUp); }} okText="替换" cancelText="保留草稿">输入框中已有内容，是否替换为建议问题？</Modal>
    <Drawer title="调查发现" open={findingsOpen} onClose={() => setFindingsOpen(false)} width="min(680px, 100vw)" destroyOnClose>{investigation && <InvestigationPanel key={investigation.id} investigation={investigation} disabled={blocked} onFollowUp={followUp} onError={setError} report={latestResult?.kind === 'report' ? latestResult : undefined} />}</Drawer>
    <Modal title={branch ? '新建调查分支' : '新建调查'} open={creatingInvestigation} onCancel={() => setCreatingInvestigation(false)} onOk={() => { void createInvestigation(); }} cancelText={"取消"} confirmLoading={busy} okText="固定范围并创建" okButtonProps={{ disabled: !goal.trim() || !scope.trim() || !formSnapshotId || !!formRangeError }}><div className="investigation-form"><label>调查目标<Input.TextArea aria-label="调查目标" value={goal} maxLength={1200} onChange={e => setGoal(e.target.value)} rows={2} /></label><label>范围与已确认条件<Input.TextArea aria-label="调查范围" value={scope} maxLength={1200} onChange={e => setScope(e.target.value)} rows={3} /></label><label>数据快照<Select aria-label="新调查数据快照" value={formSnapshotId} onChange={id => { setFormSnapshotId(id); setFormDateRange(snapshotRange(snapshots.find(item => item.id === id))); }} options={snapshots.map(item => ({ value: item.id, label: `${item.period ?? (item.periods ?? []).join('、')} · ${item.row_count.toLocaleString()} 条 · ${item.id.slice(-8)}` }))} /></label><DateRangeFields snapshot={formSnapshot} value={formDateRange} onChange={setFormDateRange} />{formSnapshot && <SnapshotSummary snapshot={formSnapshot} />}<p>创建时固定当前知识版本。分支记录来源调查，证据需在新调查中重新核查；不会自动继承旧结论或重置原调查额度。</p></div></Modal>
  </>;
}
