import type { components } from './schema';
import { knowledgeApi, jsonRequest } from './knowledge';

export type Report = components['schemas']['ReportDTO'];
export type ReportSummary = components['schemas']['ReportSummary'];
export type ReportBlock = components['schemas']['RenderedBlock'];
export type FrozenValue = components['schemas']['FrozenValue'];
export type Evidence = components['schemas']['EvidenceDTO'];
export type Selection = { block_id: string; row?: number; column?: string; value_key?: string };
export const reportApi = knowledgeApi;
export { jsonRequest };

/** 作用：从只读引用创建服务端受限的取证参数；输入：块和单元格选择；输出：查询串。 */
export function evidenceQuery(selection: Selection): string {
  return new URLSearchParams(Object.entries(selection).map(([key, value]) => [key, String(value)])).toString();
}

/** 作用：仅将冻结数值转换为绘图坐标；输入：原值；输出：有限坐标或缺失，不计算指标。 */
export function chartNumber(raw: unknown): number | null {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null;
  if (typeof raw !== 'object' || raw === null || !('type' in raw) || !('value' in raw) || !['integer', 'decimal'].includes(String(raw.type))) return null;
  if (typeof raw.value !== 'string' || !raw.value.trim()) return null;
  const number = Number(raw.value);
  return Number.isFinite(number) ? number : null;
}
