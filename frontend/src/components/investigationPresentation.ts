import type { components } from '../api/schema';
import type { FindingEvidence } from './InvestigationPanel';
type Finding = components['schemas']['FindingDTO'];

/** 修正关系使用完整调查判断，逐轮内容始终保留原记录。 */
export function findingGroups(findings: Finding[], runId?: string) {
  const supersededBy = new Map<string, Finding>();
  for (const item of findings) if (item.supersedes) supersededBy.set(item.supersedes, item);
  const items = findings.filter(item => runId ? item.run_id === runId : !supersededBy.has(item.id));
  const history = runId ? [] : findings.filter(item => supersededBy.has(item.id));
  const questions = new Map<string, boolean>();
  for (const item of items) for (const text of item.questions) {
    questions.set(text, (questions.get(text) ?? true) && supersededBy.has(item.id));
  }
  return { items, history, supersededBy, questions: [...questions].map(([text, historical]) => ({ text, historical })) };
}

/** 冻结池还含检索命中；展示只沿已有块、查询和指标绑定展开固定原文。 */
export function boundFindingEvidence(evidence: FindingEvidence): FindingEvidence {
  const ids = new Set(evidence.block.evidence_ids ?? []);
  for (const { artifact } of Object.values(evidence.artifacts)) {
    for (const id of [...(artifact.knowledge_revision_ids ?? []), ...(artifact.external_evidence_ids ?? [])]) ids.add(id);
    for (const metricId of artifact.metric_revision_ids ?? []) {
      for (const id of evidence.metrics?.[metricId]?.definition?.source_revision_ids ?? []) ids.add(id);
    }
  }
  for (;;) {
    const before = ids.size;
    for (const [id, source] of Object.entries(evidence.sources)) if (ids.has(id) || ids.has(source.revision_id ?? id)) {
      ids.add(id); ids.add(source.revision_id ?? id);
      if (Array.isArray(source.locator.citation_chunk_ids)) for (const chunk of source.locator.citation_chunk_ids) if (typeof chunk === 'string') ids.add(chunk);
    }
    if (before === ids.size) break;
  }
  return { ...evidence, sources: Object.fromEntries(Object.entries(evidence.sources).filter(([id]) => ids.has(id))) };
}
