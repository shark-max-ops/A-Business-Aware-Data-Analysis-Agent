import type { components } from './schema';

export type DocumentMetadata = components['schemas']['DocumentMetadata'];
export type Chunk = components['schemas']['ChunkDTO'];
export type Citation = components['schemas']['Citation'];
export type MetricDefinition = components['schemas']['MetricDefinition'];
export type Job = components['schemas']['JobDTO'];
export type SearchResult = components['schemas']['SearchResult'];
export type Revision = components['schemas']['DocumentRevisionDTO'];
export type GraphResult = components['schemas']['GraphResult'];
export type GraphNode = components['schemas']['GraphNode'];
// 目录 DTO 与后端 ItemList 中显式返回的字段同步；修订正文使用生成的契约。
export type DocumentItem = { id: string; title: string; version: number; current_revision_id: string; metadata: DocumentMetadata; job: Job | null };
export type WikiPayload = { title: string; content: string; citations: Citation[]; input_revision_ids: string[]; state: 'draft' | 'published'; edit_source: string };
export type WikiRevision = { id: string; page_id: string; version: number; payload: WikiPayload; created_at: string };
export type WikiPage = { id: string; slug: string; version: number; current_revision_id: string; published_revision_id: string | null; revision: WikiRevision };
export type MetricRevision = { id: string; metric_key: string; version: number; definition: MetricDefinition; created_at: string };
export type MetricItem = { key: string; version: number; current_revision_id: string; approved_revision_id: string | null; revision: MetricRevision };
export type Snapshot = { id: string; document_revision_ids: string[]; wiki_revision_ids: string[]; metric_revision_ids: string[] };

/** 作用：统一知识接口和安全错误展示；输入：相对 API 路径与请求；输出：契约 JSON。 */
export async function knowledgeApi<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
  const body = await response.json();
  if (!response.ok) throw new Error(`${body.error?.message ?? '操作失败'}：${body.error?.code ?? response.status}${body.error?.details?.current_version !== undefined ? '，请刷新后再编辑' : ''}`);
  return body as T;
}

/** 作用：构建 JSON 请求；输入：请求体和方法；输出：fetch 参数。 */
export function jsonRequest(body: unknown, method = 'POST'): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}
