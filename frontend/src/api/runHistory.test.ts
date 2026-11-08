import { describe, expect, it, vi } from 'vitest';
import { createReadQueue, readEventHistory, investigationRuns, readFrozen } from './runHistory';
import type { Run, RunEvent } from './analysis';

describe('连续调查历史', () => {
  it('固定依据合并重复读取，失败不进入成功缓存', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'TEST', message: '失败' } }), { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ fixed: true })));
    vi.stubGlobal('fetch', fetchMock);
    try {
      await expect(readFrozen('/fixed-test-evidence')).rejects.toThrow('失败');
      const [a, b] = await Promise.all([readFrozen('/fixed-test-evidence'), readFrozen('/fixed-test-evidence')]);
      expect(a).toEqual({ fixed: true }); expect(a).toBe(b);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally { vi.unstubAllGlobals(); }
  });
  it('只选择所属调查，按时间稳定排序，独立运行单独查看', () => {
    const runs = [
      { id: 'b', investigation_id: 'i', created_at: '2026-01-01' },
      { id: 'c', investigation_id: 'other', created_at: '2025-01-01' },
      { id: 'a', investigation_id: 'i', created_at: '2026-01-01' },
      { id: 'old', investigation_id: null, created_at: '2025-01-01' },
    ] as Run[];
    expect(investigationRuns(runs, 'i').map(r => r.id)).toEqual(['a', 'b']);
    expect(investigationRuns(runs, null).map(r => r.id)).toEqual(['old']);
  });
  it('读取超过一页的日志，保留部分结果并从失败页重试', async () => {
    const event = (seq: number) => ({ run_id: 'r', seq }) as RunEvent;
    const received: RunEvent[] = [];
    let fail = true;
    const cursors: number[] = [];
    const read = async (after: number) => {
      cursors.push(after);
      if (!after) return { items: Array.from({ length: 100 }, (_, i) => event(i + 1)), next_cursor: '100' };
      if (fail) throw new Error('offline');
      return { items: [event(101)], next_cursor: null };
    };
    await expect(readEventHistory(read, [], events => received.splice(0, received.length, ...events))).rejects.toThrow('offline');
    expect(received).toHaveLength(100);
    fail = false;
    const all = await readEventHistory(read, received, () => {});
    expect(all).toHaveLength(101);
    expect(cursors).toEqual([0, 100, 100]);
  });
  it('读取队列限制并发，失败后仍释放名额', async () => {
    const queue = createReadQueue(3);
    let running = 0; let peak = 0;
    const results = await Promise.allSettled(Array.from({ length: 8 }, (_, i) => queue(async () => {
      running++; peak = Math.max(peak, running);
      await new Promise(resolve => setTimeout(resolve, 1));
      running--;
      if (!i) throw new Error('failed');
      return i;
    })));
    expect(peak).toBe(3);
    expect(results.filter(r => r.status === 'fulfilled')).toHaveLength(7);
  });
});
