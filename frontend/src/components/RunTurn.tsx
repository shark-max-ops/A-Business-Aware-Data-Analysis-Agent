import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, Table } from 'antd';
import { analysisApi, eventTypes, mergeEvents, terminal } from '../api/analysis';
import type { Run, RunEvent } from '../api/analysis';
import type { components } from '../api/schema';
import { queuedRead, readEventHistory } from '../api/runHistory';
import { ExecutionTrace } from './ExecutionTrace';
import { readRunEventPage } from '../api/eventLog';
import { runFailureDescription } from './executionPresentation';
import { RunDetails } from './RunDetails';
import type { Investigation } from './InvestigationPanel';

const ReportPreview = lazy(() => import('./ReportsPage').then(module => ({ default: module.ReportPreview })));
type Artifact = components['schemas']['ArtifactPage'];

/** 仅显示冻结单元格，不在浏览器重算指标。 */
function displayCell(value: unknown): string {
  if (value === null) return '无可用值';
  if (value && typeof value === 'object' && 'type' in value && 'value' in value) return String(value.value);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

/** 每轮独立维护读取状态；仅活动运行订阅 SSE，历史按需分页读取。 */
export function RunTurn({ run, findingCount, onUpdate, onActivity, onFindings, investigation, snapshot, disabled, onFollowUp, onLocateRun, investigationError }: {
  run: Run; findingCount: number; onUpdate: (run: Run) => void; onActivity: () => void; onFindings: () => void;
  investigation?: Investigation; snapshot?: components['schemas']['SnapshotDTO']; disabled: boolean;
  onFollowUp: (question: string) => void; onLocateRun: (id: string) => void; investigationError?: string;
}) {
  const root = useRef<HTMLElement>(null);
  const alive = useRef(true);
  const eventsRef = useRef<RunEvent[]>([]);
  const reading = useRef(false);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [records, setRecords] = useState<'idle' | 'loading' | 'partial' | 'complete'>('idle');
  const [recordError, setRecordError] = useState('');
  const [connection, setConnection] = useState('');
  const [visible, setVisible] = useState(false);
  const active = !terminal(run.status);

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { root: root.current?.closest('.analysis-thread'), rootMargin: '300px' });
    if (root.current) observer.observe(root.current);
    return () => observer.disconnect();
  }, []);

  async function loadRecords() {
    if (reading.current || records === 'complete' || active) return;
    reading.current = true; setRecords('loading'); setRecordError('');
    try {
      await readEventHistory(after => {
        if (!alive.current) return Promise.reject(new Error('轮次已离开，停止读取后续记录'));
        return readRunEventPage(run.id, after);
      }, eventsRef.current, value => {
        if (alive.current) { eventsRef.current = value; setEvents(value); }
      });
      if (alive.current) setRecords('complete');
    } catch (error) {
      if (alive.current) { setRecords('partial'); setRecordError((error as Error).message); }
    } finally { reading.current = false; }
  }

  useEffect(() => {
    if (!active) return;
    let current = true;
    const stream = new EventSource(`/api/v1/runs/${run.id}/events?after=${Math.max(0, ...eventsRef.current.map(e => e.seq))}`);
    const refresh = () => void analysisApi<Run>(`/runs/${run.id}`).then(updated => {
      if (!current) return;
      onUpdate(updated);
      if (terminal(updated.status)) stream.close();
    }).catch(error => { if (current) setConnection(error.message); });
    const receive = (message: MessageEvent) => {
      if (!current) return;
      try {
        const event = JSON.parse(message.data) as RunEvent;
        if (event.run_id !== run.id || !Number.isInteger(event.seq) || !eventTypes.includes(event.type)) throw new Error();
        eventsRef.current = mergeEvents(eventsRef.current, event); setEvents(eventsRef.current);
        if (event.type === 'tool.finished') onActivity();
        if (['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.type)) {
          stream.close(); setConnection(''); setRecords('complete'); refresh(); onActivity();
        }
      } catch { setConnection('进度响应不符合契约，请重新读取执行记录'); }
    };
    eventTypes.forEach(type => stream.addEventListener(type, receive as EventListener));
    stream.onopen = () => { if (current) { setConnection('进度已连接'); refresh(); } };
    stream.onerror = () => { if (current) { setConnection('进度连接中断，正在自动重连；后端运行会继续'); refresh(); } };
    refresh();
    return () => { current = false; stream.close(); };
  }, [active, run.id, onUpdate, onActivity]);

  return <article ref={root} className="run-result" id={`turn-${run.id}`} aria-label={`分析轮次：${run.question}`}>
    <div className="analysis-question"><span>你的问题 · <time>{new Date(run.created_at).toLocaleString('zh-CN', { hour12: false })}</time></span><h3>{run.question}</h3></div>
    <ExecutionTrace run={run} events={events} connection={connection} recordsState={active ? 'live' : records} recordError={recordError} onReadRecords={() => { void loadRecords(); }} />
    {recordError && <Alert type="warning" message="执行记录尚未完整读取" description={recordError} action={<Button onClick={() => { void loadRecords(); }}>重试记录</Button>} />}
    {run.error_code && <><Alert type={run.status === 'cancelled' ? 'info' : 'error'} message={run.status === 'cancelled' ? '本次分析已取消' : run.status === 'interrupted' ? '分析因服务中断而停止' : '本次分析未完成'} description={runFailureDescription(run)} /><p className="analysis-result-note">{investigationError ? '调查记录读取失败，暂不能确认本轮已保存发现数量。' : findingCount ? <>本轮已保存 {findingCount} 条调查发现。<Button type="link" onClick={onFindings}>查看调查发现</Button></> : '本轮未保存调查发现。'}已保存的调查发现可供后续核查；本轮完整过程不会自动作为下一轮上下文。</p></>}
    {run.status === 'completed' && run.result?.kind === 'report' && (visible ? <Suspense fallback={<p>正在加载报告…</p>}><ReportPreview reportId={run.result.report_id} revisionId={run.result.revision_id} /></Suspense> : <p>报告已保存，滚动至此查看摘要。</p>)}
    {run.result?.kind === 'clarification' && <Alert type="info" message={run.result.question} description={run.result.missing_requirements.join('；')} />}
    {run.status === 'completed' && run.result?.kind === 'query_result' && <><p className="analysis-result-note">以下为本次冻结结果；执行与引用有效，业务语义未自动验证。</p>{visible && run.result.artifact_ids.map(id => <QueryPreview key={id} runId={run.id} artifactId={id} />)}</>}
    <RunDetails run={run} investigation={investigation} snapshot={snapshot} disabled={disabled} onFollowUp={onFollowUp} onLocateRun={onLocateRun} investigationError={investigationError} onRetryInvestigation={onActivity} />
  </article>;
}

/** 产物分页独立于其他轮次；失败仅影响本项，卸载后忽略迟到响应。 */
function QueryPreview({ runId, artifactId }: { runId: string; artifactId: string }) {
  const [page, setPage] = useState<Artifact | null>(null);
  const [error, setError] = useState('');
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let alive = true;
    setLoading(true); setError('');
    void queuedRead(() => analysisApi<Artifact>(`/runs/${runId}/artifacts/${artifactId}?offset=${offset}&limit=20`))
      .then(value => { if (alive) setPage(value); }).catch(reason => { if (alive) setError(reason.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [runId, artifactId, offset, attempt]);
  return <div className="artifact-preview">
    {error && <Alert type="error" message="查询结果读取失败" description={error} action={<Button onClick={() => setAttempt(a => a + 1)}>重试结果</Button>} />}
    {!page && !error && <p>正在读取查询结果…</p>}
    {page && <><h4>查询结果 · {page.artifact.returned_rows} 行</h4>{page.artifact.truncated && <Alert type="warning" message="结果已截断，不能作为完整统计" />}<Table loading={loading} size="small" pagination={false} scroll={{ x: true }} rowKey="_row" dataSource={page.rows.map((row, index) => Object.fromEntries([['_row', String(index)], ...row.map((cell, col) => [String(col), cell])]))} columns={page.artifact.columns.map((column, index) => ({ title: column.name, dataIndex: String(index), render: displayCell }))} /><p>当前显示 {page.rows.length} 行，起始位置 {page.offset + (page.rows.length ? 1 : 0)}</p>{(page.offset > 0 || page.next_offset !== null) && <Space><Button size="small" disabled={loading || page.offset === 0} onClick={() => setOffset(Math.max(0, page.offset - 20))}>上一页</Button><Button size="small" disabled={loading || page.next_offset === null} onClick={() => { if (page.next_offset !== null) setOffset(page.next_offset); }}>下一页</Button></Space>}</>}
  </div>;
}
