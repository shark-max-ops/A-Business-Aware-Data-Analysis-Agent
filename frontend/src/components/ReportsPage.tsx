import { dateRangeLabel } from '../api/data';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Alert, Button, Empty, Input, Modal, Select, Spin, Table, Tag } from 'antd';
import { jsonRequest, reportApi } from '../api/reports';
import type { FrozenValue, Report, ReportBlock, ReportSummary, Selection } from '../api/reports';
import { EvidenceDrawer } from './EvidenceDrawer';
import { ReportChart } from './ReportChart';
import { readFrozen } from '../api/runHistory';

type ReportList = { items: ReportSummary[]; next_cursor?: string | null };

/** 作用：显示冻结报告块，所有数值直接使用服务端显示值；输入：块及取证回调；输出：正文块。 */
function ReportBlockView({ block, inspect }: { block: ReportBlock; inspect: (selection: Selection) => void }) {
  if (block.type === 'kpi') return <article className="report-kpi"><p>{block.title}</p><button onClick={() => inspect({ block_id: block.id })} className="kpi-number" aria-label={`${block.title}：${block.value?.display}，查看依据`}>{block.value?.display}</button><span>点击数字查看依据 ↗</span></article>;
  if (block.type === 'text') return <section className="report-text">{(block.segments ?? []).map((segment, index) => segment.value ? <button className="evidence-value" key={index} onClick={() => inspect({ block_id: block.id, value_key: segment.key ?? undefined })}>{segment.text}</button> : <span key={index}>{segment.text}</span>)}{(block.row_bindings ?? []).length > 0 && <small> 已校验所声明的分组与数值来自同一行；文字解释仍需复核。</small>}{(block.evidence_ids ?? []).length > 0 && <button className="source-link" onClick={() => inspect({ block_id: block.id })}>查看引用资料 ↗</button>}</section>;
  return <section className="report-block"><div className="report-block-heading"><h2>{block.title}</h2><Button type="text" size="small" onClick={() => inspect({ block_id: block.id })}>查看依据 ↗</Button></div>{block.type === 'chart' ? <ReportChart block={block} inspect={inspect} /> : <Table size="middle" pagination={(block.rows?.length ?? 0) > 20 ? { pageSize: 20, showSizeChanger: false } : false} scroll={{ x: 'max-content' }} rowKey="_row" dataSource={(block.rows ?? []).map((row, index) => Object.fromEntries([['_row', index], ...row.map((cell, col) => [String(col), cell])]))} columns={(block.columns ?? []).map((column, index) => ({ title: column.label, dataIndex: String(index), render: (value: FrozenValue) => <button className="evidence-value" onClick={() => inspect({ block_id: block.id, row: value.value_ref.row, column: column.name })}>{value.display}</button> }))} locale={{ emptyText: '这个范围没有查询结果' }} />}</section>;
}

/** 作用：组合报告阅读布局与按需依据抽屉；输入：固定报告和预览开关；输出：只读正文。 */
export function ReportContent({ report, compact = false }: { report: Report; compact?: boolean }) {
  const [selection, setSelection] = useState<Selection | null>(null);
  useEffect(() => setSelection(null), [report.revision_id]);
  const kpis = report.blocks.filter(block => block.type === 'kpi');
  const content = report.blocks.filter(block => block.type !== 'kpi');
  return <div className={compact ? 'report-content report-preview' : 'report-content'}>
    {compact && report.limitations.some(note => note.startsWith('包含数据探索结果')) && <p className="report-note">数据探索 · 未绑定已审核项目指标，统计范围见结果依据。</p>}
    {kpis.length > 0 && <div className="report-kpi-grid">{(compact ? kpis.slice(0, 3) : kpis).map(block => <ReportBlockView key={block.id} block={block} inspect={setSelection} />)}</div>}
    {(compact ? content.filter(block => block.type === 'text').slice(0, 1) : content).map(block => <ReportBlockView key={block.id} block={block} inspect={setSelection} />)}
    {!compact && <section className="report-limitations"><h2>范围与限制</h2><ul>{report.limitations.map((note, index) => <li key={index}>{note}</li>)}</ul></section>}
    <EvidenceDrawer report={report} selection={selection} close={() => setSelection(null)} />
  </div>;
}

/** 作用：在对话中读取已提交报告预览；输入：报告引用；输出：摘要及报告入口。 */
export function ReportPreview({ reportId, revisionId }: { reportId: string; revisionId: string }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    setReport(null); setError('');
    void readFrozen<Report>(`/reports/${reportId}/revisions/${revisionId}`)
      .then(value => { if (alive) setReport(value); }).catch(reason => { if (alive) setError(reason.message); });
    return () => { alive = false; };
  }, [reportId, revisionId, attempt]);
  if (error) return <Alert type="error" message="报告读取失败" description={error} action={<Button onClick={() => setAttempt(a => a + 1)}>重试报告</Button>} />;
  if (!report) return <Spin tip="正在读取报告"><div className="evidence-loading" /></Spin>;
  return <section className="analysis-report-preview"><div className="report-preview-heading"><div><p className="eyebrow">报告已保存</p><h2>{report.title}</h2></div><Link className="report-open-link" to={`/reports/${report.report_id}/revisions/${report.revision_id}`}>打开完整报告 ↗</Link></div><ReportContent report={report} compact /><p className="report-note">数字取自获授权的成功查询，点击可查看固定依据。</p></section>;
}

/** 作用：分页展示真实已保存报告；输入：无；输出：报告列表。 */
export function ReportsPage() {
  const [items, setItems] = useState<ReportSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>('');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  /** 作用：读取一页报告，失败时保留当前页；输入：偏移；输出：追加列表。 */
  async function load(after: string) {
    setBusy(true); setError('');
    try {
      const result = await reportApi<ReportList>(`/reports?cursor=${after || '0'}`);
      setItems(current => after ? [...current, ...result.items] : result.items); setCursor(result.next_cursor ?? null);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  useEffect(() => { void load(''); }, []);
  return <div className="reports-page"><div className="page-heading"><div><p className="eyebrow">已保存的分析</p><h1>每个数字，都能找到依据</h1><p>报告保留生成时的数据与资料版本，随时打开，仍是当时的结果。</p></div><Link className="report-open-link" to="/analysis">开始新分析 ↗</Link></div>
    {error && <Alert type="error" message="报告列表读取失败" description={error} action={<Button onClick={() => { void load(''); }}>重试</Button>} />}
    {!items.length && !busy && !error && <section className="panel"><Empty description="还没有保存的报告"><Link to="/analysis">提出一个问题，生成第一份报告</Link></Empty></section>}
    <div className="report-list">{items.map(item => <Link className="report-list-card" key={item.id} to={`/reports/${item.id}/revisions/${item.revision_id}`}><div className="report-card-meta"><span>{dateRangeLabel(item.date_range, item.period)} · {item.source_kind === 'synthetic' ? '合成测试数据' : '固定数据版本'}</span><span>版本 {item.version}</span></div><h2>{item.title}</h2><p>{item.scope}</p><div className="report-card-bottom"><span>{new Date(item.created_at).toLocaleString('zh-CN', { hour12: false })}</span><strong>阅读报告 ↗</strong></div></Link>)}</div>
    {busy && <div className="report-loading"><Spin /> 正在读取报告…</div>}
    {cursor && !busy && <Button onClick={() => { void load(cursor); }}>加载更多报告</Button>}
  </div>;
}

/** 作用：读取固定报告、历史和显式修订；输入：路由 ID；输出：报告阅读页。 */
export function ReportDetailPage() {
  const { reportId, revisionId } = useParams();
  const navigate = useNavigate();
  const [report, setReport] = useState<Report | null>(null);
  const [history, setHistory] = useState<ReportSummary[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState('');
  const historyGeneration = useRef(0);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState('');
  const [title, setTitle] = useState('');
  const [scope, setScope] = useState('');
  const [limitations, setLimitations] = useState('');
  useEffect(() => {
    let alive = true;
    setReport(null); setError(''); setHistory([]); setEditing(false); setHistoryCursor(null); setHistoryError(''); setHistoryBusy(false);
    historyGeneration.current += 1;
    void Promise.all([reportApi<Report>(`/reports/${reportId}/revisions/${revisionId}`), reportApi<ReportList>(`/reports/${reportId}/revisions`)])
      .then(([value, revisions]) => { if (alive) { setReport(value); setHistory(revisions.items); setHistoryCursor(revisions.next_cursor ?? null); } })
      .catch(reason => { if (alive) setError(reason.message); });
    return () => { alive = false; };
  }, [reportId, revisionId, attempt]);
  /** 作用：按需读取更早版本并防止切换报告后混入旧响应；输入：服务端游标；输出：追加版本。 */
  async function olderVersions() {
    if (!report || !historyCursor || historyBusy) return;
    const generation = historyGeneration.current;
    setHistoryBusy(true); setHistoryError('');
    try {
      const value = await reportApi<ReportList>(`/reports/${report.report_id}/revisions?cursor=${historyCursor}`);
      if (generation === historyGeneration.current) { setHistory(current => [...current, ...value.items]); setHistoryCursor(value.next_cursor ?? null); }
    } catch (reason) { if (generation === historyGeneration.current) setHistoryError((reason as Error).message); }
    finally { if (generation === historyGeneration.current) setHistoryBusy(false); }
  }
  /** 作用：只修改可读说明并生成新版本；输入：表单及加载时版本；输出：新修订页面。 */
  async function save() {
    if (!report) return;
    setSaving(true); setEditError('');
    try {
      const updated = await reportApi<Report>(`/reports/${report.report_id}/revisions`, jsonRequest({
        expected_version: report.version, report: { ...report.draft, title: title.trim(), scope: scope.trim(), limitations: limitations.split('\n').map(value => value.trim()).filter(Boolean) },
      }));
      setEditing(false); navigate(`/reports/${updated.report_id}/revisions/${updated.revision_id}`);
    } catch (reason) { setEditError((reason as Error).message); }
    finally { setSaving(false); }
  }
  if (error) return <Alert type="error" message="报告无法打开" description={error} action={<Button onClick={() => setAttempt(value => value + 1)}>重试</Button>} />;
  if (!report) return <div className="report-loading"><Spin /> 正在打开报告…</div>;
  const base = `/api/v1/reports/${report.report_id}/revisions/${report.revision_id}`;
  const latest = Math.max(...history.map(item => item.version), report.version);
  const newest = history.find(item => item.version === latest);
  // 阅读区使用已知字段的中文称谓；原始范围与版本仍保存在报告和依据中。
  const scopeText = report.scope.replaceAll(report.data_snapshot.id, '（已固定）')
    .replace(/v_flights\s*表/g, '航班运行表').replace(/\bv_flights\b/g, '航班运行表')
    .replace(/\bflight_date\b/g, '航班日期').replace(/（COUNT\(\*\)）/g, '');

  return <div className="report-reader">
    {historyError && <Alert type="error" message="更早版本读取失败" description={historyError} />}
    <div className="report-toolbar"><Link to="/reports">← 所有报告</Link><div className="report-actions"><Select aria-label="报告版本" dropdownRender={menu => <>{menu}{historyCursor && <Button type="text" block loading={historyBusy} onMouseDown={event => event.preventDefault()} onClick={() => { void olderVersions(); }}>加载更早版本</Button>}</>} value={report.revision_id} options={history.map(item => ({ value: item.revision_id, label: `版本 ${item.version}${item.version === latest ? ' · 最新' : ''}` }))} onChange={id => navigate(`/reports/${report.report_id}/revisions/${id}`)} /><a href={base + '/export?format=markdown'}>导出 Markdown</a><a href={base + '/export?format=json'}>导出 JSON</a><Button disabled={report.version < latest} onClick={() => { setTitle(report.title); setScope(report.scope); setLimitations((report.draft.limitations ?? []).join('\n')); setEditError(''); setEditing(true); }}>修改说明</Button></div></div>
    {newest && report.version < latest && <p className="report-note" role="status">当前为历史版本。<Link to={`/reports/${report.report_id}/revisions/${newest.revision_id}`}>查看最新版本 {latest} ↗</Link></p>}
    <article className="report-sheet"><header className="report-header"><div className="report-meta"><span>{dateRangeLabel(report.date_range, report.data_snapshot.period)}</span><span>{report.data_snapshot.manifest?.source_kind === 'synthetic' ? '合成测试数据' : '固定数据版本'}</span><span>版本 {report.version}</span></div><h1>{report.title}</h1><p className="report-scope">{scopeText}</p><p className="report-origin">分析问题：{report.question}</p><div className="report-stamp"><Tag color="green">数字与引用已核对</Tag><span>{new Date(report.created_at).toLocaleString('zh-CN', { hour12: false })}</span></div></header>
      <ReportContent report={report} />
    </article>
    <Modal title="修改报告说明" open={editing} onCancel={() => { if (!saving) setEditing(false); }} onOk={() => { void save(); }} confirmLoading={saving} okText="保存为新版本" cancelText="取消" destroyOnClose>
      <p className="report-note">保留原版本，数字仍来自同一批已保存的查询结果。</p>{editError && <Alert type="error" message="没有保存修改" description={editError} />}
      <div className="report-edit-fields"><label htmlFor="report-edit-title">报告标题</label><Input id="report-edit-title" value={title} maxLength={160} onChange={event => setTitle(event.target.value)} /><label htmlFor="report-edit-scope">统计范围说明</label><Input.TextArea id="report-edit-scope" rows={3} value={scope} maxLength={1000} onChange={event => setScope(event.target.value)} /><label htmlFor="report-edit-limitations">限制与补充说明（每行一条）</label><Input.TextArea id="report-edit-limitations" rows={4} value={limitations} onChange={event => setLimitations(event.target.value)} /></div>
    </Modal>
  </div>;
}
