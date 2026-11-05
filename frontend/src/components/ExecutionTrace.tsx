import { useEffect, useId, useState } from 'react';
import { Alert, Drawer, Select } from 'antd';
import type { Run, RunEvent } from '../api/analysis';
import { terminal } from '../api/analysis';
import { executionSteps, executionSummary, formatDuration, modelSteps, stepDuration, recordOf, runFailureDescription, stepDescription, toolLabels, toolStatusLabels } from './executionPresentation';
import type { ToolStatus, ToolStep } from './executionPresentation';
import { ExplorationNote } from './ExplorationNote';

type Tone = ToolStatus | 'success' | 'warning' | 'error' | 'neutral';

/** 作用：显示轻量执行状态；输入：真实状态；输出：装饰图标。 */
function StateMark({ tone }: { tone: Tone }) {
  return <span className={`execution-mark ${tone}`} aria-hidden="true">{tone === 'running' ? <span className="execution-spinner" /> : <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{['success', 'succeeded'].includes(tone) ? <path d="m4 10 4 4 8-8" /> : ['failed', 'error'].includes(tone) ? <><path d="m6 6 8 8M14 6l-8 8" /></> : ['warning', 'interrupted', 'unknown'].includes(tone) ? <><path d="M10 5v6" /><path d="M10 15h.01" /></> : <path d="M5 10h10" />}</svg>}</span>;
}

/** 作用：展示真实工具状态且不把缺失结束记录标为完成；输入：步骤及运行；输出：状态。 */
function visibleStatus(step: ToolStep, run: Run): ToolStatus {
  return terminal(run.status) && step.status === 'running' ? 'unknown' : step.status;
}

/** 作用：以文字渲染结构化原始记录；输入：任意 JSON；输出：可滚动代码块。 */
function JsonBlock({ value }: { value: unknown }) {
  return <pre className="execution-code"><code>{JSON.stringify(value, null, 2)}</code></pre>;
}

/** 作用：显示按需展开的执行过程；输入：固定运行、真实事件与连接提示；输出：行内摘要和详情抽屉。 */
export function ExecutionTrace({ run, events, connection }: { run: Run; events: RunEvent[]; connection: string }) {
  const [expanded, setExpanded] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [selectedId, setSelectedId] = useState<string>();
  const contentId = useId();
  const [now, setNow] = useState(Date.now);
  const scopedEvents = events.filter(event => event.run_id === run.id);
  const steps = executionSteps(scopedEvents);
  const timeline = [...steps, ...modelSteps(scopedEvents)].sort((a, b) => (a.startedAt ?? a.finishedAt ?? '').localeCompare(b.startedAt ?? b.finishedAt ?? ''));
  const summary = executionSummary(run, scopedEvents, steps);
  const resultIds = run.result?.kind === 'query_result' ? run.result.artifact_ids : [];
  const preferred = [...steps].reverse().find(step => resultIds.includes(String(recordOf(recordOf(step.result?.data).artifact).id))) ?? steps.at(-1);
  const selected = steps.find(step => step.id === selectedId) ?? preferred;
  const data = recordOf(selected?.result?.data);
  const artifact = recordOf(data.artifact);
  const sql = typeof selected?.arguments.sql === 'string' ? selected.arguments.sql : typeof artifact.sql === 'string' ? artifact.sql : null;
  const params = selected?.arguments.params ?? artifact.parameters ?? {};
  const sources = Array.isArray(data.items) ? data.items.map(recordOf) : [];
  const reconnecting = connection.includes('中断');

  useEffect(() => { setExpanded(false); setDrawer(false); setSelectedId(undefined); }, [run.id]);
  // 运行结束时自动收起；用户之后仍可以主动展开，重放事件不会反复关闭。
  useEffect(() => { if (terminal(run.status)) setExpanded(false); }, [run.status]);
  useEffect(() => {
    if (terminal(run.status)) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [run.id, run.status]);

  /** 作用：打开选中工具的真实记录；输入：可选执行 ID；输出：详情抽屉。 */
  function inspect(id?: string) {
    setSelectedId(id);
    setDrawer(true);
  }

  return <div className={`execution-trace ${summary.tone}`}>
    <div className="execution-summary">
      <button type="button" className="execution-toggle" aria-expanded={expanded} aria-controls={contentId} onClick={() => setExpanded(value => !value)}>
        <svg className={`execution-chevron ${expanded ? 'expanded' : ''}`} aria-hidden="true" viewBox="0 0 16 16"><path d="m6 3 5 5-5 5" /></svg>
        <StateMark tone={summary.tone} />
        <span className="execution-summary-copy"><span className="execution-title">{summary.title}</span><span className="execution-meta">{summary.info}</span></span>
      </button>
      <button type="button" className="execution-detail-link" onClick={() => inspect()}>执行详情 <span aria-hidden="true">↗</span></button>
    </div>
    <span className="sr-only" role="status">{summary.title}</span>
    {reconnecting && <p className="execution-connection" role="status">进度连接已断开，正在重连；后端运行会继续。</p>}
    {expanded && <div id={contentId} role="region" aria-label="工具步骤" className="execution-steps">
      {timeline.length === 0 && <p className="execution-empty">{terminal(run.status) ? '本次运行没有工具执行记录。' : '正在等待实际工具执行。'}</p>}
      {timeline.map(step => step.name === 'model_thinking' ? <div key={step.id} className={`execution-step ${visibleStatus(step, run)}`}>
        <StateMark tone={visibleStatus(step, run)} />
        <span className="execution-step-copy"><span className="execution-step-label">模型思考<span className="execution-step-duration" title="从模型请求进度至下一阶段事件，包含响应整理；不展示模型内部思维。">{stepDuration(step, run, now)}</span></span></span>
        <span className="execution-step-state">{toolStatusLabels[visibleStatus(step, run)]}</span>
      </div> : <button key={step.id} type="button" className={`execution-step ${visibleStatus(step, run)}`} onClick={() => inspect(step.id)}>
        <StateMark tone={visibleStatus(step, run)} />
        <span className="execution-step-copy"><span className="execution-step-label">{toolLabels[step.name] ?? step.name}<span className="execution-step-duration">{stepDuration(step, run, now)}</span></span><span className="execution-step-description">{visibleStatus(step, run) === 'unknown' ? '未收到完整的结束记录' : stepDescription(step)}</span></span>
        <span className="execution-step-state">{toolStatusLabels[visibleStatus(step, run)]}</span>
        <span className="execution-step-arrow" aria-hidden="true">↗</span>
      </button>)}
    </div>}

    <Drawer title="执行详情" open={drawer} onClose={() => setDrawer(false)} width="min(600px, 100vw)" className="execution-drawer">
      <p className="execution-drawer-eyebrow">本次分析</p><h3 className="execution-drawer-question">{run.question}</h3>
      <div className={`execution-overview ${summary.tone}`}><StateMark tone={summary.tone} /><span>{summary.title}</span><span className="execution-meta">{summary.info}</span></div>
      <p className="execution-fixed-note">数据与知识版本已固定。以下仅展示实际操作与结果。</p>
      {run.error_code && <Alert type={run.status === 'cancelled' ? 'info' : 'warning'} showIcon message={summary.title} description={<><p>{runFailureDescription(run)}</p><code>{run.error_code}</code></>} />}
      {steps.length > 0 ? <>
        <label className="execution-field-label" htmlFor={`${contentId}-tool`}>工具记录</label>
        <Select id={`${contentId}-tool`} aria-label="选择工具记录" value={selected?.id} onChange={setSelectedId} style={{ width: '100%' }} options={steps.map((step, index) => ({ value: step.id, label: `${index + 1}. ${toolLabels[step.name] ?? step.name} · ${toolStatusLabels[visibleStatus(step, run)]}` }))} />
        {selected && <section className="execution-tool-detail" aria-label="所选工具详情">
          <div className="execution-tool-heading"><h4>{toolLabels[selected.name] ?? selected.name}</h4><span className={`execution-tool-status ${visibleStatus(selected, run)}`}>{toolStatusLabels[visibleStatus(selected, run)]}</span></div>
          <p className="execution-detail-description">{stepDescription(selected)}</p>
          <ExplorationNote artifact={artifact} />
          {selected.durationMs !== null && <p className="execution-meta">工具用时 {formatDuration(selected.durationMs)} · 按服务端开始与结束事件计算</p>}
          {typeof selected.result?.error_code === 'string' && <Alert type="warning" message="本次工具未成功" description={String(selected.result.error_code)} />}
          {sql && <><h4>SQL</h4><pre className="execution-code"><code>{sql}</code></pre><h4>绑定参数</h4>{Object.keys(recordOf(params)).length ? <JsonBlock value={params} /> : <p className="execution-meta">无绑定参数</p>}</>}
          {sources.length > 0 && <div className="execution-sources"><h4>命中的来源资料</h4>{sources.map((source, index) => <div key={String(source.chunk_id ?? index)}><span className="execution-meta">{({ official_reference: '官方资料', project_policy: '项目规则', generated_wiki: '生成 Wiki' } as Record<string, string>)[String(source.source_kind)] ?? '来源资料'}</span><p>{typeof source.excerpt === 'string' ? source.excerpt : ''}</p></div>)}</div>}
          {typeof artifact.id === 'string' && <details className="execution-technical"><summary>查询产物与依据</summary><JsonBlock value={artifact} /></details>}
          <details className="execution-technical"><summary>工具输入</summary><JsonBlock value={selected.arguments} /></details>
          <details className="execution-technical"><summary>工具返回结果</summary>{selected.result ? <JsonBlock value={selected.result} /> : <p>工具尚未返回结果。</p>}</details>
        </section>}
      </> : <p className="execution-empty">本次运行尚无工具记录。</p>}
      <details className="execution-technical"><summary>固定版本与运行信息</summary><JsonBlock value={{ run_id: run.id, snapshot_id: run.snapshot_id, knowledge_snapshot_id: run.knowledge_snapshot_id, metric_revision_ids: run.metric_revision_ids, model: run.model }} /></details>
      <details className="execution-technical"><summary>原始事件 · {scopedEvents.length} 条</summary><JsonBlock value={scopedEvents} /></details>
    </Drawer>
  </div>;
}
