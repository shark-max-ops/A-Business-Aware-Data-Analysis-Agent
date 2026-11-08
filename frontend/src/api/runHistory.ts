import type { Run, RunEvent } from './analysis';
import { analysisApi, mergeEvents } from './analysis';

/** 按调查归属选择运行；空调查仅代表旧独立运行，排序不改变服务端记录。 */
export function investigationRuns(runs: Run[], investigationId: string | null): Run[] {
  return runs.filter(run => (run.investigation_id ?? null) === investigationId)
    .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
}

/** 顺序读取现有日志接口；逐页交付，失败时调用方保留已读内容供重试。 */
export async function readEventHistory(
  read: (after: number) => Promise<{ items: RunEvent[]; next_cursor?: string | null }>,
  initial: RunEvent[], onPage: (events: RunEvent[]) => void,
): Promise<RunEvent[]> {
  let events = initial;
  let after = Math.max(0, ...events.map(event => event.seq));
  for (;;) {
    const page = await read(after);
    for (const event of page.items) events = mergeEvents(events, event);
    onPage(events);
    if (!page.next_cursor) return events;
    const next = Number(page.next_cursor);
    if (!Number.isInteger(next) || next <= after) throw new Error('事件分页游标未前进');
    after = next;
  }
}

/** 限制报告及查询读取并发；不用于创建、发送或取消等写请求。 */
export function createReadQueue(limit: number) {
  let running = 0;
  const pending: (() => void)[] = [];
  return async function queued<T>(read: () => Promise<T>): Promise<T> {
    await new Promise<void>(resolve => {
      const start = () => { running++; resolve(); };
      if (running < limit) start(); else pending.push(start);
    });
    try { return await read(); }
    finally { running--; pending.shift()?.(); }
  };
}
export const queuedRead = createReadQueue(3);

const frozenReads = new Map<string, Promise<unknown>>();
/** 仅用于固定报告修订及冻结依据；同标识复用成功读取，失败后允许重试。 */
export function readFrozen<T>(path: string): Promise<T> {
  let pending = frozenReads.get(path);
  if (!pending) {
    pending = queuedRead(() => analysisApi<T>(path)).catch(error => { frozenReads.delete(path); throw error; });
    frozenReads.set(path, pending);
  }
  return pending as Promise<T>;
}
