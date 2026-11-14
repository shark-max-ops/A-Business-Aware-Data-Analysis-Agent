import { describe, expect, it } from 'vitest';
import type { Run, RunEvent } from '../api/analysis';
import { executionSteps, executionSummary, modelSteps, stepDuration, runFailureDescription, stepDescription } from './executionPresentation';

const run: Run = { id: 'run_test', conversation_id: 'conv_test', question: '核对范围', snapshot_id: 'snapshot_test', knowledge_snapshot_id: 'ks_test', status: 'completed', budget: {}, context_run_ids: [], metric_revision_ids: [], result: { kind: 'query_result', artifact_ids: ['qa_test'] }, error_code: null, cancel_requested: false, created_at: '2026-10-31T00:00:00Z', seq: 5, usage: {}, model: 'isolated-test' };
/** 作用：构造明确的合成事件；输入：序号、类型和负载；输出：契约事件。 */
function event(seq: number, type: RunEvent['type'], payload: RunEvent['payload']): RunEvent {
  return { run_id: run.id, seq, type, created_at: `2026-10-31T00:00:0${seq}Z`, payload };
}
const history: RunEvent[] = [
  event(1, 'run.started', {}),
  event(2, 'tool.started', { tool_execution_id: 'exec_bad', tool: 'query_sql', arguments: { sql: 'SELECT wrong_column' } }),
  event(3, 'tool.finished', { tool_execution_id: 'exec_bad', tool: 'query_sql', status: 'failed', ok: false, error_code: 'SQL_EXECUTION_FAILED', data: {} }),
  event(4, 'tool.started', { tool_execution_id: 'exec_good', tool: 'query_sql', arguments: { sql: 'SELECT COUNT(*) FROM v_flights' } }),
  event(5, 'tool.finished', { tool_execution_id: 'exec_good', tool: 'query_sql', status: 'succeeded', ok: true, data: { artifact: { returned_rows: 1, truncated: false } } }),
];

describe('execution presentation uses actual evidence', () => {
  it('restores model phases from replay and keeps tool time separate', () => {
    const start = event(1, 'run.progress', { turn: 1 });
    const events = [history[2], start, history[1], start, event(4, 'run.progress', { turn: 2 }), event(5, 'run.failed', {})];
    const phases = modelSteps(events);
    expect(phases).toHaveLength(2);
    expect(phases.map(step => step.durationMs)).toEqual([1000, 1000]);
    expect(phases[1].status).toBe('failed');
    expect(stepDuration(executionSteps(events)[0], run, Date.now())).toBe('1s');
  });
  it('ticks active phases but never invents a duration for incomplete terminal history', () => {
    const step = modelSteps([event(1, 'run.progress', { turn: 1 })])[0];
    const now = Date.parse('2026-10-31T00:00:19Z');
    expect(stepDuration(step, { ...run, status: 'running' }, now)).toBe('18s');
    expect(stepDuration(step, run, now)).toBe('用时未知');
    expect(stepDuration({ ...step, startedAt: 'invalid' }, { ...run, status: 'running' }, now)).toBe('用时未知');
    expect(stepDuration({ ...step, durationMs: 150 }, run, now)).toBe('<1s');
  });
  it('distinguishes a saved report from an unfinished weather check', () => {
    const events = [event(1, 'run.started', {}), event(2, 'tool.started', { tool_execution_id: 'weather', tool: 'query_historical_weather', arguments: { airport: 'ATL', start: '2026-01-24' } }), event(3, 'tool.finished', { tool_execution_id: 'weather', tool: 'query_historical_weather', status: 'failed', ok: false, error_code: 'EXTERNAL_TIMEOUT' }), event(5, 'run.completed', {})];
    const report: Run = { ...run, result: { kind: 'report', report_id: 'synthetic', revision_id: 'synthetic', partial: false, has_unverified_explanations: false } };
    const steps = executionSteps(events);
    expect(executionSummary(report, events, steps).title).toBe('报告已生成，天气核查未完成');
    expect(stepDescription(steps[0])).toContain('本次未完成');
    expect(executionSummary(report, history, executionSteps(history)).title).toBe('报告已生成');
  });
  it('labels retained results as partial instead of full analysis success', () => {
    const partial: Run = { ...run, result: { kind: 'report', report_id: 'synthetic', revision_id: 'synthetic', partial: true, has_unverified_explanations: false } };
    const summary = executionSummary(partial, history, executionSteps(history));
    expect(summary.title).toBe('部分完成，已保留核查结果');
    expect(summary.tone).toBe('warning');
  });
  it('does not imply unverified narrative is a checked conclusion', () => {
    const value: Run = { ...run, result: { kind: 'report', report_id: 'synthetic', revision_id: 'synthetic', partial: false, has_unverified_explanations: true } };
    const summary = executionSummary(value, history, executionSteps(history));
    expect(summary.title).toBe('结果已保存，附有未核实解释');
    expect(summary.tone).toBe('warning');
  });
  it('explains model failures without blaming successful SQL or inventing old diagnostics', () => {
    const failed = { ...run, status: 'failed' as const, error_code: 'MODEL_REQUEST_FAILED' };
    expect(runFailureDescription(failed)).toContain('无法确定更具体');
    expect(runFailureDescription({ ...failed, error_details: { failure_kind: 'timeout', attempts: 3 } })).toContain('响应超时。已尝试 3 次');
    expect(runFailureDescription({ ...failed, error_code: 'MODEL_RESPONSE_INVALID' })).toContain('无法解析');
    expect(runFailureDescription({ ...failed, error_code: 'MODEL_HTTP_ERROR', error_details: { status: 401, attempts: 1 } })).toContain('凭据');
    expect(runFailureDescription({ ...failed, error_code: 'TURN_BUDGET_EXCEEDED' })).toContain('不表示你的提问有误');
    expect(runFailureDescription({ ...failed, error_code: 'TOOL_BUDGET_EXCEEDED' })).toContain('工具调用额度');
    expect(runFailureDescription({ ...failed, error_code: 'CONTEXT_BUDGET_EXCEEDED' })).toContain('历史或工具结果');
  });
  it('pairs replayed events by execution ID and preserves failed attempts', () => {
    const steps = executionSteps([history[4], ...history, history[2]]);
    expect(steps).toHaveLength(2);
    expect(steps.map(step => step.status)).toEqual(['failed', 'succeeded']);
    expect(steps[0].arguments.sql).toBe('SELECT wrong_column');
    expect(steps[1].durationMs).toBe(1000);
    expect(stepDescription(steps[1])).toBe('返回 1 行');
    const summary = executionSummary(run, history, steps);
    expect(summary.info).toContain('2 次工具调用');
    expect(summary.info).toContain('1 次失败记录');
    expect(summary.tone).toBe('warning');
  });
  it('does not present clarification as a successful query', () => {
    const clarification: Run = { ...run, result: { kind: 'clarification', question: '请审核指标', missing_requirements: ['口径'] } };
    const summary = executionSummary(clarification, history, executionSteps(history));
    expect(summary.title).toBe('需要补充条件');
    expect(summary.tone).toBe('warning');
  });
  it('keeps failure, cancellation and interruption distinct', () => {
    for (const [status, title] of [['failed', '分析未完成'], ['cancelled', '分析已取消'], ['interrupted', '分析已中断']] as const) {
      const summary = executionSummary({ ...run, status }, history, executionSteps(history));
      expect(summary.title).toBe(title);
      expect(summary.tone).not.toBe('success');
    }
  });
  it('does not invent timings or completed steps for incomplete history', () => {
    const pending = [event(2, 'tool.started', { tool_execution_id: 'pending', tool: 'query_sql', arguments: {} })];
    const steps = executionSteps(pending);
    expect(steps[0].status).toBe('running');
    expect(steps[0].durationMs).toBeNull();
    const summary = executionSummary({ ...run, status: 'running' }, pending, steps);
    expect(summary.durationMs).toBeNull();
    expect(summary.title).toBe('正在执行只读查询');
    expect(summary.info).toContain('正在同步执行记录');
    const incomplete = [...pending, event(5, 'run.completed', { result_kind: 'query_result' })];
    expect(executionSummary(run, incomplete, steps).tone).toBe('warning');
  });
  it('marks result truncation and missing reviewed rules in short tool summaries', () => {
    const steps = executionSteps(history);
    expect(stepDescription({ ...steps[1], result: { data: { artifact: { returned_rows: 20, truncated: true } } } })).toContain('结果已截断');
    expect(stepDescription({ ...steps[0], result: { error_code: 'METRIC_NOT_APPLICABLE' } })).toBe('没有适用的已审核指标');
    expect(stepDescription({ ...steps[0], result: { error_code: 'REPORT_ROW_BINDING_MISMATCH' } })).toContain('不在同一行');
    expect(stepDescription({ ...steps[0], result: { error_code: 'REPORT_ROW_NOT_FOUND' } })).toContain('结果行不存在');
  });
});
