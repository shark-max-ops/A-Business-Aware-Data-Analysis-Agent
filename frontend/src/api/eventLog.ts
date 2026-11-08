import type { RunEvent } from './analysis';
import { eventTypes } from './analysis';

/** 只读现有事件接口；区分网络、HTTP、解析及契约错误，保留诊断状态。 */
export async function readRunEventPage(runId: string, after: number): Promise<{ items: RunEvent[]; next_cursor: string | null }> {
  let response: Response;
  try { response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/event-log?after=${after}`); }
  catch { throw new Error('无法连接执行记录服务，请检查本地服务与网络后重试'); }
  let body;
  try { body = await response.json(); }
  catch { throw new Error(`响应无法解析（HTTP ${response.status}），请检查服务或开发代理`); }
  if (!response.ok) throw new Error(`HTTP ${response.status} · ${body?.error?.code ?? 'EVENT_LOG_FAILED'}：${body?.error?.message ?? '执行记录请求失败'}`);
  if (!body || !Array.isArray(body.items) || !(body.next_cursor === null || typeof body.next_cursor === 'string') || body.items.some((event: RunEvent) =>
    !event || event.run_id !== runId || !Number.isInteger(event.seq) || event.seq <= after || !eventTypes.includes(event.type)
    || typeof event.created_at !== 'string' || !event.payload || typeof event.payload !== 'object')) {
    throw new Error('服务响应不符合执行记录契约，请核对运行与接口版本');
  }
  return body;
}
