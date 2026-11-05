import type { Run, RunEvent } from '../api/analysis';

export type ToolStatus = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted' | 'unknown';
export type ToolStep = {
  id: string;
  name: string;
  status: ToolStatus;
  arguments: Record<string, unknown>;
  result: Record<string, unknown> | null;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
};

export const toolLabels: Record<string, string> = {
  query_historical_weather: '核查天气观测', search_official_advisories: '查找运行公告', read_external_source: '核对外部原文',
  read_investigation: '回顾调查记录', record_finding: '保存发现与依据',
  search_knowledge: '查阅来源资料', read_wiki_page: '阅读已发布 Wiki', resolve_metric: '核对指标口径',
  list_tables: '确认可查询表', describe_table: '读取表结构', query_sql: '执行只读查询', read_artifact: '读取查询结果',
};
const runningLabels: Record<string, string> = {
  query_historical_weather: '正在核查官方天气观测', search_official_advisories: '正在查找运行公告', read_external_source: '正在核对外部原文与范围',
  read_investigation: '正在回顾调查记录', record_finding: '正在核对并保存发现',
  search_knowledge: '正在查阅来源资料', read_wiki_page: '正在阅读 Wiki', resolve_metric: '正在核对指标口径',
  list_tables: '正在确认可查询表', describe_table: '正在读取表结构', query_sql: '正在执行只读查询', read_artifact: '正在读取查询结果',
};
export const toolStatusLabels: Record<ToolStatus, string> = {
  running: '执行中', succeeded: '已完成', failed: '未成功', cancelled: '已取消', interrupted: '已中断', unknown: '状态未知',
};

/** 作用：解释真实模型失败并兼容缺少细分诊断的历史运行；输入：运行；输出：中文说明。 */
export function runFailureDescription(run: Run): string {
  if (run.error_code === 'CONTEXT_BUDGET_EXCEEDED') return '整理的历史或工具结果超过了本次上下文容量，当前运行未完成。已保存的报告与发现仍保留；这不表示你的提问有误。';
  if (run.error_code === 'TURN_BUDGET_EXCEEDED') return '本次模型交互轮数已用完，尚未完成最终结果。已保存的查询和发现仍保留；这不表示你的提问有误。';
  if (run.error_code === 'TOOL_BUDGET_EXCEEDED') return '本次工具调用额度已用完，尚未完成最终结果。已保存的查询和发现仍保留。';
  const details = run.error_details ?? {};
  const attempts = Number(details.attempts ?? 0);
  const tried = attempts > 1 ? `已尝试 ${attempts} 次，仍未恢复。` : '';
  if (run.error_code === 'MODEL_REQUEST_FAILED') {
    const reason = details.failure_kind === 'timeout' ? '等待模型服务响应超时。'
      : details.failure_kind === 'connection' ? '未能连接到模型服务。'
      : details.failure_kind === 'connection_interrupted' ? '与模型服务的连接或响应读取中断。'
      : '模型请求未完成，现有记录无法确定更具体的连接或响应原因。';
    return `${reason}${tried}已保存的查询与阶段性发现仍保留，本轮尚未生成完整报告。`;
  }
  if (run.error_code === 'MODEL_RESPONSE_INVALID' || run.error_code === 'MODEL_PROTOCOL_ERROR') return '模型服务返回了无法解析或不符合协议的响应，本轮尚未完成。请查看执行详情后重新发起。';
  if (run.error_code === 'MODEL_HTTP_ERROR') return [401, 403].includes(Number(details.status))
    ? '模型服务拒绝了请求，请检查本机凭据与访问权限。'
    : `模型服务返回异常状态${details.status ? `（${details.status}）` : ''}。${tried}请稍后重新发起。`;
  return '可修改问题后重新发起，具体原因可在执行详情中查看。';
}

/** 作用：安全读取工具对象；输入：事件中的未知数据；输出：普通对象或空对象。 */
export function recordOf(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/** 作用：计算真实事件时间间隔；输入：服务端开始及结束时间；输出：毫秒或未知。 */
function elapsed(start: string | null, end: string | null): number | null {
  if (!start || !end) return null;
  const duration = Date.parse(end) - Date.parse(start);
  return Number.isFinite(duration) && duration >= 0 ? duration : null;
}

/** 作用：还原模型请求阶段，不读取内部思维；输入：真实进度事件；输出：模型阶段及事件边界耗时。 */
export function modelSteps(events: RunEvent[]): ToolStep[] {
  const ordered = [...new Map(events.map(event => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq);
  return ordered.flatMap((event, index) => {
    if (event.type !== 'run.progress' || typeof event.payload.turn !== 'number') return [];
    const end = ordered.slice(index + 1).find(next => ['run.progress', 'tool.started', 'report.ready', 'run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(next.type));
    const status: ToolStatus = !end ? 'running' : end.type === 'run.failed' ? 'failed' : end.type === 'run.cancelled' ? 'cancelled' : end.type === 'run.interrupted' ? 'interrupted' : 'succeeded';
    return [{ id: `model-${event.seq}`, name: 'model_thinking', status, arguments: {}, result: null, startedAt: event.created_at, finishedAt: end?.created_at ?? null, durationMs: elapsed(event.created_at, end?.created_at ?? null) }];
  });
}

/** 作用：显示完成耗时或正在增长的用时，缺失记录不造数；输入：步骤、运行及当前时间；输出：紧凑用时。 */
export function stepDuration(step: ToolStep, run: Run, now: number): string {
  let duration = step.durationMs;
  if (duration === null && step.status === 'running' && ['queued', 'running'].includes(run.status) && step.startedAt) {
    const start = Date.parse(step.startedAt);
    if (Number.isFinite(start)) duration = Math.max(0, now - start);
  }
  if (duration === null) return '用时未知';
  return duration < 1000 ? '<1s' : `${Math.floor(duration / 1000)}s`;
}

/** 作用：按执行 ID 配对事件，保留失败尝试；输入：运行事件；输出：按实际开始顺序排列的工具步骤。 */
export function executionSteps(events: RunEvent[]): ToolStep[] {
  const steps = new Map<string, ToolStep>();
  // 重放可能包含重复或乱序事件，只按 seq 还原，不能把不同尝试合并成一次成功。
  const seen = new Set<number>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (seen.has(event.seq) || !['tool.started', 'tool.finished'].includes(event.type)) continue;
    seen.add(event.seq);
    const payload = event.payload;
    if (typeof payload.tool_execution_id !== 'string') continue;
    const id = payload.tool_execution_id;
    const step = steps.get(id) ?? {
      id, name: typeof payload.tool === 'string' ? payload.tool : '未知工具', status: 'unknown' as ToolStatus,
      arguments: {}, result: null, startedAt: null, finishedAt: null, durationMs: null,
    };
    if (event.type === 'tool.started') {
      step.startedAt = event.created_at;
      step.arguments = recordOf(payload.arguments);
      step.status = 'running';
    } else {
      step.finishedAt = event.created_at;
      step.result = payload;
      step.status = ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(String(payload.status))
        ? payload.status as ToolStatus : 'unknown';
    }
    step.durationMs = elapsed(step.startedAt, step.finishedAt);
    steps.set(id, step);
  }
  return [...steps.values()];
}

/** 作用：按真实状态生成紧凑标题；输入：运行及配对事件；输出：文字、色调和运行总用时。 */
export function executionSummary(run: Run, events: RunEvent[], steps: ToolStep[]) {
  const complete = events.some(event => event.seq >= run.seq);
  const failed = steps.filter(step => step.status === 'failed').length;
  const running = [...steps].reverse().find(step => step.status === 'running');
  const weather = steps.filter(step => step.name === 'query_historical_weather');
  const weatherKey = (step: ToolStep) => JSON.stringify(Object.entries(step.arguments).sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => [key, key === 'fields' && Array.isArray(value) ? [...value].sort() : value]));
  const pendingWeather = weather.some((step, index) => step.status === 'failed' && !weather.slice(index + 1).some(later => later.status === 'succeeded' && weatherKey(later) === weatherKey(step)));
  const started = events.find(event => event.type === 'run.started');
  const finished = events.find(event => ['run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'].includes(event.type));
  let title: string;
  let tone: 'running' | 'success' | 'warning' | 'error' | 'neutral';
  switch (run.status) {
    case 'queued': title = '分析已排队'; tone = 'running'; break;
    case 'running': title = run.cancel_requested ? '正在停止分析' : running ? runningLabels[running.name] ?? '正在执行工具' : '正在处理分析请求'; tone = 'running'; break;
    case 'completed':
      title = run.result?.kind === 'clarification' ? '需要补充条件' : run.result?.kind === 'report' ? '报告已生成' : '查询已完成';
      if (run.result?.kind === 'report' && pendingWeather) title = '报告已生成，天气核查未完成';
      tone = run.result?.kind === 'clarification' || failed ? 'warning' : 'success';
      break;
    case 'failed': title = '分析未完成'; tone = 'error'; break;
    case 'cancelled': title = '分析已取消'; tone = 'neutral'; break;
    case 'interrupted': title = '分析已中断'; tone = 'warning'; break;
  }
  if (run.status === 'completed' && ['query_result', 'report'].includes(run.result?.kind ?? '')) {
    if (!complete) tone = 'neutral';
    else if (steps.some(step => ['running', 'unknown'].includes(step.status))) tone = 'warning';
  }
  const info = [complete ? `${steps.length} 次工具调用` : '正在同步执行记录'];
  if (failed) info.push(`含 ${failed} 次失败记录`);
  const durationMs = elapsed(started?.created_at ?? null, finished?.created_at ?? null);
  if (durationMs !== null) info.push(formatDuration(durationMs));
  return { title, tone, info: info.join(' · '), durationMs };
}

/** 作用：显示实际测得用时；输入：毫秒；输出：紧凑用时文本。 */
export function formatDuration(value: number): string {
  return value < 1000 ? `${Math.round(value)} 毫秒` : `${(value / 1000).toFixed(1)} 秒`;
}

/** 作用：展示工具的实际结果摘要；输入：配对步骤；输出：短文字。 */
export function stepDescription(step: ToolStep): string {
  if (step.status !== 'succeeded') {
    if (step.status === 'running') return '等待工具返回';
    const code = step.result?.error_code;
    const messages: Record<string, string> = {
      EXTERNAL_TIMEOUT: '外部核查超时，本次未完成', EXTERNAL_CONNECTION_FAILED: '外部来源暂时连接失败', EXTERNAL_HTTP_ERROR: '外部来源未能读取', WEATHER_STATION_NOT_FOUND: '尚无已核对的气象站', EXTERNAL_SOURCE_NOT_GRANTED: '来源未获本轮授权', EXTERNAL_REQUEST_LIMIT: '外部请求额度已用完', EXTERNAL_BYTE_BUDGET_EXCEEDED: '外部资料读取量达到上限',
      REPORT_TEMPLATE_KEYS: '发现文字与数值引用不匹配，需修正', EXTERNAL_TOOL_NOT_SUCCEEDED: '来源对应工具未成功，需重新核查',
      METRIC_NOT_APPLICABLE: '没有适用的已审核指标', METRIC_CONFLICT: '指标口径存在冲突',
      SQL_EXECUTION_FAILED: '查询未成功，未生成证据', INVALID_ARGUMENT: '工具参数需要修正',
      RUN_CANCELLED: '执行已取消', QUERY_CANCELLED: '查询已取消', RUN_INTERRUPTED: '服务中断',
      QUERY_TIMEOUT: '查询超时', METRIC_REVIEW_OR_CLARIFICATION_REQUIRED: '需要先审核指标或补充条件',
    };
    return typeof code === 'string' && messages[code] ? messages[code] : toolStatusLabels[step.status];
  }
  const data = recordOf(step.result?.data);
  if (step.name === 'query_historical_weather') return `${recordOf(data.mapping).airport ?? ''} · 观测与来源已固定`;
  if (step.name === 'search_official_advisories') return `找到 ${data.total ?? 0} 条候选${data.partial ? '，部分日期未完成' : ''}，尚需核对正文`;
  if (step.name === 'read_external_source') return data.source_kind === 'official_advisory' ? '原文已固定，保留时段及航司限制' : '固定外部版本已读取';
  if (step.name === 'list_tables' && Array.isArray(data.tables)) return `${data.tables.length} 张可查询表`;
  const table = recordOf(data.table);
  if (step.name === 'describe_table' && typeof table.name === 'string') return `${table.name}${Array.isArray(table.columns) ? ` · ${table.columns.length} 个字段` : ''}`;
  const artifact = recordOf(data.artifact);
  if (step.name === 'query_sql' && typeof artifact.returned_rows === 'number') return `返回 ${artifact.returned_rows} 行${artifact.truncated ? '，结果已截断' : ''}`;
  if (step.name === 'search_knowledge' && Array.isArray(data.items)) return `找到 ${data.items.length} 条资料`;
  if (step.name === 'read_artifact' && Array.isArray(data.rows)) return `读取 ${data.rows.length} 行`;
  const definition = recordOf(data.definition);
  if (typeof definition.name === 'string') return definition.name;
  const payload = recordOf(data.payload);
  return typeof payload.title === 'string' ? payload.title : '工具已返回结果';
}
