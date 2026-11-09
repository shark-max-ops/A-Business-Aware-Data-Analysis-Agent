import { dateRangeLabel } from '../api/data';
import { useEffect, useRef, useState } from 'react';
import { Button, Empty, Modal, Tag } from 'antd';
import type { components } from '../api/schema';
import { ExplorationNote } from './ExplorationNote';
import { ExternalSource, isExternalSource } from './ExternalSource';
import { boundFindingEvidence, findingGroups } from './investigationPresentation';
import { readFrozen } from '../api/runHistory';

export type Investigation = components['schemas']['InvestigationDTO'];
type Finding = components['schemas']['FindingDTO'];
export type FindingEvidence = {
  block: { evidence_ids?: string[]; segments: { text: string; value?: { display: string; value_ref: { artifact_id: string; row: number; column: string } } }[] };
  artifacts: Record<string, { artifact: components['schemas']['QueryArtifact'] }>;
  sources: Record<string, { revision_id?: string; title: string; content: string; source_kind: string; locator: Record<string, unknown> }>;
  metrics?: Record<string, { definition?: { source_revision_ids?: string[] } }>;
};
const kindText: Record<string, string> = {
  fact: '观察事实', candidate: '待核查线索', counterevidence: '反向证据', limitation: '当前限制', recommendation: '后续建议',
};

/** 展示调查发现与核查入口；版本及额度由独立信息抽屉呈现。 */
export function InvestigationPanel({ investigation: inv, disabled, onFollowUp, runId, onLocateRun }: {
  investigation: Investigation; disabled: boolean; onFollowUp: (question: string) => void; onError: (message: string) => void;
  runId?: string; onLocateRun?: (id: string) => void;
}) {
  const [evidence, setEvidence] = useState<FindingEvidence | null>(null);
  const [error, setError] = useState('');
  const [selectedFinding, setSelectedFinding] = useState<Finding | null>(null);
  const [loading, setLoading] = useState('');
  const alive = useRef(true);
  const request = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const { items: current, history, questions, supersededBy } = findingGroups(inv.findings, runId);

  /** 作用：按服务端固定发现读取证据；输入：发现；输出：证据弹窗或真实错误。 */
  async function showEvidence(finding: Finding) {
    const current = ++request.current;
    setLoading(finding.id); setError(''); setEvidence(null); setSelectedFinding(finding);
    try {
      const value = await readFrozen<FindingEvidence>(`/investigations/${inv.id}/findings/${finding.id}/evidence`);
      if (alive.current && current === request.current) setEvidence(boundFindingEvidence(value));
    } catch (error) { if (alive.current && current === request.current) setError((error as Error).message); }
    finally { if (alive.current && current === request.current) setLoading(''); }
  }

  /** 作用：展示已物化的发现；输入：发现及历史标记；输出：可核对卡片。 */
  function findingView(item: Finding, historical = false) {
    return <article className={`investigation-finding${historical ? ' historical' : ''}`} key={item.id}>
      <div className="finding-heading"><Tag color={item.kind === 'counterevidence' ? 'gold' : item.kind === 'fact' ? 'green' : undefined}>{kindText[item.kind] ?? item.kind}</Tag>{historical && <span>已有后续修正</span>}{['failed', 'cancelled', 'interrupted'].includes(item.source_run_status) && <span>来源运行未完成 · 复用前需重新核查</span>}</div>
      <p>{item.text}</p><div className="finding-footer"><span>核查：{item.check}</span><Button size="small" type="link" loading={loading === item.id} onClick={() => { void showEvidence(item); }}>查看依据</Button></div>
      {historical && onLocateRun && supersededBy.get(item.id) && <Button type="link" size="small" onClick={() => onLocateRun(supersededBy.get(item.id)!.run_id)}>定位后续修正轮次</Button>}
    </article>;
  }

  return <section className="investigation-panel" aria-label={runId ? '本轮调查发现' : '当前调查'}>
    <div className="investigation-findings"><h3>{runId ? '本轮发现' : '发现与待核查事项'}</h3>{current.length ? current.map(item => findingView(item, supersededBy.has(item.id))) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={runId ? '本轮尚无已保存的调查发现' : '核查后的发现、反证和限制会保存在这里'} />}</div>
    {history.length > 0 && <details className="investigation-meta"><summary>查看被修正的历史记录</summary>{history.map(item => findingView(item, true))}</details>}
    {questions.length > 0 && <div className="investigation-next"><h3>可以继续核查</h3>{questions.map(question => <div key={question.text}>{question.historical && <span className="historical-note">历史建议 · 原发现已有后续修正</span>}<Button disabled={disabled || inv.status === 'budget_exhausted'} onClick={() => onFollowUp(question.text)}>{question.text}</Button></div>)}</div>}
    <p className="investigation-footnote">引用和数字经服务端校验；业务解释与因果关系仍需复核。</p>
    <Modal title="发现的固定依据" centered styles={{ body: { maxHeight: 'calc(100dvh - 200px)', overflowY: 'auto' } }} open={!!selectedFinding} onCancel={() => { request.current++; setSelectedFinding(null); setLoading(''); }} footer={<Button aria-label="关闭" onClick={() => { request.current++; setSelectedFinding(null); setLoading(''); }}>关闭</Button>} width={800}>
      {loading && <p role="status">正在读取固定依据…</p>}
      {error && <div><p role="alert">依据读取失败：{error}</p><Button onClick={() => { if (selectedFinding) void showEvidence(selectedFinding); }}>重试依据</Button></div>}
      {evidence && <div className="finding-evidence"><p>{evidence.block.segments.map(s => s.text).join('')}</p>
        {evidence.block.segments.filter(s => s.value).map((s, index) => <p key={index}>引用值：{s.value!.display} · 第 {s.value!.value_ref.row + 1} 行 · {s.value!.value_ref.column}</p>)}
        {Object.entries(evidence.artifacts).map(([id, item]) => <details key={id}><summary>成功查询 · 冻结 {item.artifact.returned_rows} 行</summary>{runId && <p>{item.artifact.run_id === runId ? '本轮产生的查询' : '引用旧轮次的证据'}</p>}<ExplorationNote artifact={item.artifact} /><pre>{item.artifact.sql}</pre><p>绑定参数：{JSON.stringify(item.artifact.parameters)}</p><code>来源运行：{item.artifact.run_id}</code></details>)}
        {Object.entries(evidence.sources).map(([id, source]) => isExternalSource(source.source_kind) ? <ExternalSource key={id} source={source} /> : <details key={id}><summary>{source.title} · {source.source_kind === 'official_reference' ? '官方资料' : source.source_kind === 'project_policy' ? '项目规则' : source.source_kind === 'wiki_generated' ? '生成 Wiki' : '来源材料'}</summary><p>定位：{JSON.stringify(source.locator)}</p><pre>{source.content}</pre></details>)}
        {!Object.keys(evidence.artifacts).length && !Object.keys(evidence.sources).length && <p>这是一项限制或建议，没有绑定数据结论。</p>}
      </div>}
    </Modal>
  </section>;
}

/** 固定版本及分配额度仅在调查信息抽屉内呈现。 */
export function InvestigationInfo({ investigation: inv }: { investigation: Investigation }) {
  return <section className="investigation-info"><h2>{inv.goal}</h2><p>{inv.scope}</p>{inv.date_range && <p>{dateRangeLabel(inv.date_range)}</p>}<Tag>{inv.status === 'running' ? '核查中' : inv.status === 'budget_exhausted' ? '累计额度已用完' : '可继续调查'}</Tag>
    <h3>版本与累计额度</h3><p>数据和知识版本在调查创建时固定。运行前分配额度，取消或失败也不退回；这不是实际 token 消耗。</p><p>已分配 {inv.allocated.runs}/{inv.limits.runs} 次运行 · {inv.allocated.max_turns}/{inv.limits.max_turns} 轮模型交互 · {inv.allocated.max_tool_calls}/{inv.limits.max_tool_calls} 次工具调用 · {inv.allocated.timeout_seconds}/{inv.limits.timeout_seconds} 秒执行额度</p><p>剩余任一额度不足以分配完整运行时不能继续。</p><code>数据：{inv.snapshot_id}<br />知识：{inv.knowledge_snapshot_id}</code>{inv.parent_id && <p>此调查由已有调查显式建立分支。</p>}</section>;
}
