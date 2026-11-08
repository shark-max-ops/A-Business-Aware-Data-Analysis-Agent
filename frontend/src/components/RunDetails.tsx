import { lazy, Suspense, useEffect, useState } from 'react';
import { Alert, Button } from 'antd';
import type { Run } from '../api/analysis';
import type { components } from '../api/schema';
import { readFrozen } from '../api/runHistory';
import { InvestigationPanel } from './InvestigationPanel';
import type { FindingEvidence, Investigation } from './InvestigationPanel';
import { ExternalSource, isExternalSource } from './ExternalSource';
import { EvidenceDrawer } from './EvidenceDrawer';
import type { Evidence, Report, Selection } from '../api/reports';
import { boundFindingEvidence } from './investigationPresentation';

const DataSourceDrawer = lazy(() => import('./DataSourceDrawer').then(module => ({ default: module.DataSourceDrawer })));
type Snapshot = components['schemas']['SnapshotDTO'];
type Finding = components['schemas']['FindingDTO'];

/** 本轮信息只使用运行固定字段；累计额度仍只属于顶部汇总。 */
export function RunDetails({ run, investigation, snapshot, disabled, onFollowUp, onLocateRun, investigationError, onRetryInvestigation }: {
  run: Run; investigation?: Investigation; snapshot?: Snapshot; disabled: boolean;
  onFollowUp: (question: string) => void; onLocateRun: (id: string) => void; investigationError?: string;
  onRetryInvestigation: () => void;
}) {
  const [dataOpen, setDataOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const findings = investigation?.findings.filter(item => item.run_id === run.id) ?? [];
  return <div className="turn-investigation">
    {investigationError && <Alert type="warning" message="调查发现读取失败" description={investigationError} action={<Button onClick={onRetryInvestigation}>刷新调查发现</Button>} />}
    {investigationError && !findings.length ? <p>本轮发现尚不能确认，请刷新调查记录。</p> : investigation ? <InvestigationPanel investigation={investigation} runId={run.id} disabled={disabled} onFollowUp={onFollowUp} onLocateRun={onLocateRun} onError={() => {}} />
      : <p>{run.investigation_id ? '本轮调查记录尚未加载。' : '本轮为历史独立运行，没有关联的调查发现记录。'}</p>}
    <details className="turn-info"><summary>本轮信息</summary>
      {investigation && <><p>所属调查目标：{investigation.goal}</p><p>所属调查固定范围：{investigation.scope}</p></>}
      <p>本轮状态：{({ queued: '已排队', running: '分析中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '服务中断' })[run.status]}</p>
      <p>创建时间：{new Date(run.created_at).toLocaleString('zh-CN', { hour12: false })}</p>
      <p>固定数据版本：<code>{run.snapshot_id}</code><br />固定知识版本：<code>{run.knowledge_snapshot_id}</code></p>
      <p>本轮预算：{Object.entries(run.budget).length ? Object.entries(run.budget).map(([key, value]) => `${({ max_turns: '模型交互轮数', max_tool_calls: '工具调用次数', timeout_seconds: '执行时限（秒）' } as Record<string, string>)[key] ?? key} ${value}`).join(' · ') : '未记录'}</p>
      <p>已记录模型用量：{run.usage && Object.keys(run.usage).length ? Object.entries(run.usage).map(([key, value]) => `${key} ${value}`).join(' · ') : '未记录'}</p>
      <p>这是本轮预算与已记录用量，调查累计分配额度见顶部“调查信息”。</p>
    </details>
    <details className="turn-sources" onToggle={event => { if (event.currentTarget.open) setSourcesOpen(true); }}><summary>本轮数据与依据</summary>
      <p>主数据：{snapshot ? `${snapshot.period} · ${snapshot.row_count.toLocaleString()} 条` : '固定快照详情尚未读取'}<br /><code>{run.snapshot_id}</code></p>
      <Button onClick={() => setDataOpen(true)}>查看本轮固定数据</Button>
      {sourcesOpen && <RunSources run={run} investigationId={investigation?.id} findings={findings} investigationError={investigationError} />}
    </details>
    {dataOpen && <Suspense fallback={<p>正在加载固定数据…</p>}><DataSourceDrawer open onClose={() => setDataOpen(false)} fixedSnapshotId={run.snapshot_id} fixedSourceId={snapshot?.source_id} /></Suspense>}
  </div>;
}

/** 固定来源仅来自本轮发现及本轮报告；每项失败独立保留，不扫描工具检索结果。 */
function RunSources({ run, investigationId, findings, investigationError }: { run: Run; investigationId?: string; findings: Finding[]; investigationError?: string }) {
  const [values, setValues] = useState<Record<string, FindingEvidence>>({});
  const [report, setReport] = useState<Report | null>(null);
  const [reportEvidence, setReportEvidence] = useState<Record<string, Evidence>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pending, setPending] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [selection, setSelection] = useState<Selection | null>(null);
  const ids = findings.map(item => item.id).join(',');
  const reportResult = run.result?.kind === 'report' ? run.result : null;
  useEffect(() => {
    let alive = true;
    setErrors({});
    const reads: { key: string; path: string; report: boolean }[] = findings.map(finding => ({ key: finding.id, path: `/investigations/${investigationId}/findings/${finding.id}/evidence`, report: false }));
    if (reportResult) reads.push({ key: 'report', path: `/reports/${reportResult.report_id}/revisions/${reportResult.revision_id}`, report: true });
    setPending(reads.length);
    for (const item of reads) {
      void readFrozen<Report | FindingEvidence>(item.path).then(async value => {
        if (!alive) return;
        if (item.report) {
          const report = value as Report;
          setReport(report);
          const selections = report.blocks.flatMap(block => {
            const byArtifact = new Map<string, string>();
            for (const segment of block.segments ?? []) if (segment.value && segment.key) byArtifact.set(segment.value.value_ref.artifact_id, segment.key);
            return [{ key: block.id, query: `block_id=${encodeURIComponent(block.id)}` }, ...[...byArtifact].map(([artifactId, valueKey]) => ({ key: `${block.id}:${artifactId}`, query: `block_id=${encodeURIComponent(block.id)}&value_key=${encodeURIComponent(valueKey)}` }))];
          });
          await Promise.all(selections.map(async selection => {
            try {
              const bound = await readFrozen<Evidence>(`${item.path}/evidence?${selection.query}`);
              if (alive) setReportEvidence(current => ({ ...current, [selection.key]: bound }));
            } catch (reason) { if (alive) setErrors(current => ({ ...current, [`block:${selection.key}`]: (reason as Error).message })); }
          }));
        }
        else setValues(current => ({ ...current, [item.key]: boundFindingEvidence(value as FindingEvidence) }));
      }).catch(reason => { if (alive) setErrors(current => ({ ...current, [item.key]: reason.message })); })
        .finally(() => { if (alive) setPending(count => count - 1); });
    }
    return () => { alive = false; };
  }, [run.id, investigationId, ids, reportResult?.report_id, reportResult?.revision_id, attempt]);
  const sources = new Map<string, { title: string; content: string; source_kind: string; locator: Record<string, unknown> }>();
  for (const value of Object.values(reportEvidence)) for (const source of value.sources) sources.set(`${source.source_kind}:${source.id}`, { ...source, locator: source.locator ?? {} });
  for (const evidence of Object.values(values)) for (const [id, source] of Object.entries(evidence.sources)) sources.set(`${source.source_kind}:${id}`, source);
  const artifacts = new Map([...Object.values(values).flatMap(value => Object.entries(value.artifacts)), ...Object.values(reportEvidence).flatMap(value => value.artifact ? [[value.artifact.id, { artifact: value.artifact }] as const] : [])]);
  return <div className="turn-source-index">
    {pending > 0 && <p role="status">正在读取本轮固定依据…</p>}
    {Object.entries(errors).map(([id, message]) => <Alert key={id} type="warning" message="部分依据读取失败" description={message} action={<Button onClick={() => setAttempt(a => a + 1)}>重试依据索引</Button>} />)}
    {artifacts.size > 0 && <h4>发现与报告引用的成功查询</h4>}
    {[...artifacts].map(([id, item]) => <details key={id}><summary>{item.artifact.run_id === run.id ? '本轮产生的查询' : '引用旧轮次的证据'} · {id}</summary><p>来源运行：<code>{item.artifact.run_id}</code></p><pre>{item.artifact.sql}</pre></details>)}
    {sources.size > 0 && <h4>发现与报告引用的固定资料</h4>}
    {[...sources].map(([key, source]) => isExternalSource(source.source_kind) ? <ExternalSource key={key} source={source} /> : <details key={key}><summary>{source.title} · {source.source_kind}</summary><p>固定标识：<code>{key}</code></p><p>定位：{JSON.stringify(source.locator)}</p><pre>{source.content}</pre></details>)}
    {report && <><h4>报告块的固定依据</h4>{report.blocks.map(block => <Button key={block.id} onClick={() => setSelection({ block_id: block.id })}>{block.title || block.id} · 查看固定依据</Button>)}<EvidenceDrawer report={report} selection={selection} close={() => setSelection(null)} /></>}
    {run.result?.kind === 'query_result' && <p>本轮返回的冻结查询：{run.result.artifact_ids.map(id => <code key={id}>{id} </code>)}。结果与分页见上方本轮查询表格。</p>}
    {investigationError && <p>调查记录读取失败，暂不能确认本轮发现引用是否完整。</p>}
    {!investigationError && !pending && !Object.keys(errors).length && !sources.size && !artifacts.size && !report && <p>本轮没有附加的发现或报告资料引用；主数据版本见上方。</p>}
    <p>仅显示已保存发现及冻结报告的引用；工具检索命中与查阅过程见执行详情。</p>
  </div>;
}
