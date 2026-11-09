import { describe, expect, it } from 'vitest';
import { dateRangeError, snapshotRange } from './data';
import type { Snapshot } from './data';

describe('date selection', () => {
  it('keeps legacy December and leap-year boundaries without local timezone conversion', () => {
    expect(snapshotRange({ period: '2026-12' } as Snapshot)).toEqual({ start: '2026-12-01', end_exclusive: '2027-01-01' });
    expect(snapshotRange({ period: '2024-02' } as Snapshot)?.end_exclusive).toBe('2024-03-01');
  });
  it('rejects missing dates, reversal, outside coverage and month gaps before submission', () => {
    const snapshot = { period: null, periods: ['2026-01', '2026-03'], coverage_start: '2026-01-01', coverage_end_exclusive: '2026-04-01' } as Snapshot;
    expect(dateRangeError(snapshot)).toContain('请选择');
    expect(dateRangeError(snapshot, { start: '2026-01-02', end_exclusive: '2026-01-02' })).toContain('晚于');
    expect(dateRangeError(snapshot, { start: '2025-12-31', end_exclusive: '2026-01-02' })).toContain('超出');
    expect(dateRangeError(snapshot, snapshotRange(snapshot))).toContain('缺少 2026-02');
    expect(dateRangeError(snapshot, { start: '2026-01-02', end_exclusive: '2026-02-01' })).toBe('');
  });
});
