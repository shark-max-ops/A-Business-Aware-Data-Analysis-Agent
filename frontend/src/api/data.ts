import type { components } from './schema';

export type Source = components['schemas']['SourceDTO'];
export type Snapshot = components['schemas']['SnapshotDTO'];
export type Schema = components['schemas']['SchemaDTO'];
export type LoadState<T> = { kind: 'loading' } | { kind: 'ready'; data: T } | { kind: 'error'; message: string };

/** 作用：展示服务端冻结范围，旧记录保留月份；输入：范围与旧月份；输出：日期说明。 */
export function dateRangeLabel(range?: components['schemas']['DateRange'] | null, period?: string | null): string {
  return range ? `${range.start} 至 ${range.end_exclusive}（不含结束日）` : period || '范围未记录';
}

export type DateRange = components['schemas']['DateRange'];

/** 作用：兼容旧单月记录推导可选范围；输入：固定快照；输出：覆盖范围。 */
export function snapshotRange(snapshot?: Snapshot): DateRange | undefined {
  if (snapshot?.coverage_start && snapshot.coverage_end_exclusive) return { start: snapshot.coverage_start, end_exclusive: snapshot.coverage_end_exclusive };
  if (!snapshot?.period) return undefined;
  const [year, month] = snapshot.period.split('-').map(Number);
  return { start: `${snapshot.period}-01`, end_exclusive: `${year + (month === 12 ? 1 : 0)}-${String(month % 12 + 1).padStart(2, '0')}-01` };
}

/** 作用：检查页面范围、覆盖与缺月；输入：快照及选择；输出：可读错误或空串。 */
export function dateRangeError(snapshot?: Snapshot, range?: DateRange): string {
  const coverage = snapshotRange(snapshot);
  if (!coverage || !range?.start || !range.end_exclusive) return '请选择开始日期和结束日期';
  if (range.start >= range.end_exclusive) return '结束日期必须晚于开始日期';
  if (range.start < coverage.start || range.end_exclusive > coverage.end_exclusive) return '日期范围超出所选快照覆盖';
  const periods = snapshot?.period ? [snapshot.period] : snapshot?.periods ?? [];
  let cursor = range.start.slice(0, 7);
  while (`${cursor}-01` < range.end_exclusive) {
    if (!periods.includes(cursor)) return `所选范围缺少 ${cursor} 数据`;
    const [year, month] = cursor.split('-').map(Number);
    cursor = `${year + (month === 12 ? 1 : 0)}-${String(month % 12 + 1).padStart(2, '0')}`;
  }
  return '';
}

/** 作用：请求真实数据 API 并转换统一错误；输入：路径与请求参数；输出：类型化响应。 */
export async function requestData<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, options);
  let body: unknown;
  try { body = await response.json(); } catch { throw new Error('服务返回了无法识别的内容。'); }
  if (!response.ok) {
    const error = body as { error?: { message?: string; code?: string } };
    throw new Error(`${error.error?.message || '数据请求失败'}（${error.error?.code || response.status}）`);
  }
  return body as T;
}
