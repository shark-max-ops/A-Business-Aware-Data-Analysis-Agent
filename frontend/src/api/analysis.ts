import type { components } from './schema';
import { knowledgeApi, jsonRequest } from './knowledge';

export type Run = components['schemas']['RunDTO'];
export type RunEvent = components['schemas']['EventDTO'];
export type Conversation = components['schemas']['ConversationDTO'];
export const analysisApi = knowledgeApi;
export { jsonRequest };
export const terminal = (status: Run['status']) => ['completed', 'failed', 'cancelled', 'interrupted'].includes(status);
export const eventTypes = ['run.queued', 'run.started', 'run.progress', 'tool.started', 'tool.finished', 'report.ready', 'run.completed', 'run.failed', 'run.cancelled', 'run.interrupted'];

/** 作用：按运行与序号合并重放事件；输入：已有记录和新事件；输出：排序后记录。 */
export function mergeEvents(current: RunEvent[], event: RunEvent): RunEvent[] {
  if (current.some(item => item.run_id === event.run_id && item.seq === event.seq)) return current;
  return [...current, event].sort((a, b) => a.seq - b.seq);
}
