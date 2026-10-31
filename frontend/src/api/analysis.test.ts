import { describe, expect, it } from 'vitest';
import { mergeEvents } from './analysis';
import type { RunEvent } from './analysis';

describe('SSE replay', () => {
  it('deduplicates by run and seq and orders history', () => {
    const event: RunEvent = { run_id: 'run_a', seq: 2, type: 'run.started', created_at: '', payload: {} };
    const same = mergeEvents([event], { ...event });
    expect(same).toHaveLength(1);
    const earlier = { ...event, seq: 1, type: 'run.queued' as const };
    expect(mergeEvents(same, earlier).map(item => item.seq)).toEqual([1, 2]);
    expect(mergeEvents(same, { ...event, run_id: 'run_b' })).toHaveLength(2);
  });
});
