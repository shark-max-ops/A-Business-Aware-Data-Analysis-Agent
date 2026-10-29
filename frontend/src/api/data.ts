import type { components } from './schema';

export type Source = components['schemas']['SourceDTO'];
export type Snapshot = components['schemas']['SnapshotDTO'];
export type Schema = components['schemas']['SchemaDTO'];
export type LoadState<T> = { kind: 'loading' } | { kind: 'ready'; data: T } | { kind: 'error'; message: string };

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
