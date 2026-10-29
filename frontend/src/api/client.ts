import type { components } from './schema';
export type Health = components['schemas']['HealthResponse'];
export type ApiError = components['schemas']['ErrorResponse'];
export type HealthState =
  | { kind: 'loading' }
  | { kind: 'ready'; health: Health }
  | { kind: 'degraded'; health: Health }
  | { kind: 'error'; message: string };

/** 作用：读取真实健康状态；输入：取消信号；输出：页面状态。 */
export async function loadHealth(signal?: AbortSignal): Promise<HealthState> {
  try {
    const response = await fetch('/api/v1/health', { signal });
    const body: unknown = await response.json();
    // 运行时校验代理响应，避免错误数据被展示成服务正常。
    if (isHealth(body) && (response.status === 200 || response.status === 503)) {
      return { kind: response.ok && body.status === 'ok' ? 'ready' : 'degraded', health: body };
    }
    if (isApiError(body)) return { kind: 'error', message: `${body.error.message}（请求 ${body.error.request_id}）` };
    return { kind: 'error', message: '服务响应不符合接口约定，请检查后端。' };
  } catch (error) {
    if (signal?.aborted) throw error;
    return { kind: 'error', message: '无法连接后端，请确认服务已启动后重试。' };
  }
}

/** 作用：验证健康响应字段；输入：未知 JSON；输出：类型判断。 */
function isHealth(value: unknown): value is Health {
  if (!value || typeof value !== 'object') return false;
  const h = value as Record<string, unknown>;
  return ['ok', 'degraded'].includes(String(h.status)) && h.stage === 1
    && ['ready', 'migration_required', 'unavailable'].includes(String(h.database))
    && ['available', 'unavailable'].includes(String(h.fts5))
    && h.worker === 'not_implemented' && h.analysis === 'not_implemented'
    && ['configured_unverified', 'not_configured'].includes(String(h.llm))
    && ['configured_unverified', 'not_configured'].includes(String(h.embedding));
}

/** 作用：识别统一错误；输入：未知 JSON；输出：类型判断。 */
function isApiError(value: unknown): value is ApiError {
  if (!value || typeof value !== 'object' || !('error' in value)) return false;
  const error = value.error;
  return !!error && typeof error === 'object' && 'message' in error && typeof error.message === 'string'
    && 'request_id' in error && typeof error.request_id === 'string';
}
