import { useEffect, useState } from 'react';
import { Alert, Drawer, Empty, Spin, Tag } from 'antd';
import { evidenceQuery, reportApi } from '../api/reports';
import type { Evidence, Report, Selection } from '../api/reports';
import type { MetricDefinition } from '../api/knowledge';
import { MetricSummary } from './MetricSummary';
import { ExplorationNote } from './ExplorationNote';

const sourceLabels: Record<string, string> = { official_reference: '官方资料', project_policy: '项目规则', wiki_generated: '生成 Wiki', mixed_document: '原文与项目注释' };

/** 作用：保留合并来源的全部实际引用行号；输入：固定来源与当前原文；输出：可读定位。 */
function sourcePlaces(sources: Evidence['sources'], source: Evidence['sources'][number]): string {
  const places = sources.flatMap(piece => {
    const first = piece.locator?.line_start;
    const last = piece.locator?.line_end;
    return piece.revision_id === source.revision_id && piece.source_kind === source.source_kind && typeof first === 'number'
      ? [`第 ${first}—${typeof last === 'number' ? last : first} 行`] : [];
  });
  return [...new Set(places)].join('；');
}

/** 作用：按点击值展示其固定原值、查询和资料；输入：报告与受限选择；输出：依据抽屉。 */
export function EvidenceDrawer({ report, selection, close }: { report: Report; selection: Selection | null; close: () => void }) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setEvidence(null); setError('');
    if (!selection) return;
    let alive = true;
    setLoading(true);
    void reportApi<Evidence>(`/reports/${report.report_id}/revisions/${report.revision_id}/evidence?${evidenceQuery(selection)}`)
      .then(value => { if (alive) setEvidence(value); })
      .catch(reason => { if (alive) setError(reason.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [report.report_id, report.revision_id, selection]);
  const sources = evidence?.sources ?? [];
  // 同类型的片段与完整原文合为一个阅读入口，原始引用位置仍可查看。
  const visibleSources = sources.filter(source => source.id === source.revision_id || !sources.some(
    parent => parent.id === source.revision_id && parent.source_kind === source.source_kind));
  return <Drawer title="这个结果从哪里来" open={selection !== null} onClose={close} width={620} className="report-evidence-drawer">
    {loading && <Spin tip="正在读取固定依据"><div className="evidence-loading" /></Spin>}
    {error && <Alert type="error" message="依据读取失败" description={error} />}
    {evidence && <div className="evidence-content">
      {evidence.value && <section className="evidence-highlight"><p>你点击的数值</p><strong>{evidence.value.display}</strong><small>原始值：{typeof evidence.value.raw === 'object' ? JSON.stringify(evidence.value.raw) : String(evidence.value.raw ?? '缺失')}</small></section>}
      <section><h3>数据与统计范围</h3><p>{evidence.data_snapshot.manifest?.source_kind === 'synthetic' ? '合成测试数据' : '固定数据快照'} · {evidence.data_snapshot.period}</p><p>来自本次分析已保存的查询结果，资料更新不会替换这里的依据。</p>
        {evidence.artifact && <p>结果共 {evidence.artifact.returned_rows} 行 · {evidence.artifact.truncated ? '已截断' : '未截断'}</p>}
      </section>
      <ExplorationNote artifact={evidence.artifact} />
      {evidence.metrics.length > 0 ? <section><h3>使用的已审核口径</h3>{evidence.metrics.map((metric, i) => <MetricSummary key={i} definition={metric.definition as MetricDefinition} documents={[]} openSource={id => { const source = document.getElementById(`report-source-${id}`); if (source instanceof HTMLDetailsElement) source.open = true; source?.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' }); }} />)}</section> : <p className="report-note">这是基础数据探索，没有绑定已审核的项目指标。</p>}
      <section><h3>查阅的固定来源</h3>{!evidence.sources.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这项结果没有附加知识资料引用" />}{visibleSources.map(source => <details id={`report-source-${source.id}`} className="evidence-source" key={source.id}><summary><Tag>{sourceLabels[source.source_kind] ?? '来源资料'}</Tag>{source.title}</summary>{typeof source.locator?.line_start === 'number' && <p>原文第 {source.locator.line_start} 行起</p>}{sourcePlaces(sources, source) && <p>引用位置：{sourcePlaces(sources, source)}</p>}<div className="source-original">{source.content}</div><details className="technical-details"><summary>来源定位与版本</summary><pre>{JSON.stringify({ revision_id: source.revision_id, locator: source.locator }, null, 2)}</pre></details></details>)}</section>
      <details className="technical-details"><summary>查看 SQL、参数与执行记录</summary>{evidence.value && <p>原始列：{evidence.value.value_ref.column} · 第 {evidence.value.value_ref.row + 1} 行</p>}<pre>{evidence.artifact?.sql ?? '此文字块未选择具体查询单元格'}</pre><pre>{JSON.stringify({ parameters: evidence.artifact?.parameters, duration_ms: evidence.artifact?.duration_ms, snapshot_id: evidence.data_snapshot.id, file_hash: evidence.data_snapshot.file_hash, result_hash: evidence.artifact?.result_hash, knowledge_snapshot_id: evidence.knowledge_snapshot_id }, null, 2)}</pre></details>
      <p className="report-note">已核对数字与引用。引用存在，不代表业务解释已经自动验证。</p>
    </div>}
  </Drawer>;
}
