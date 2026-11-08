import { describe, expect, it } from 'vitest';
import { findingGroups, boundFindingEvidence } from './investigationPresentation';
import type { FindingEvidence } from './InvestigationPanel';
import type { components } from '../api/schema';
type Finding = components['schemas']['FindingDTO'];
const findings = [
  { id: 'old', run_id: 'one', supersedes: null, questions: ['旧问题', '相同问题'] },
  { id: 'new', run_id: 'two', supersedes: 'old', questions: ['新问题'] },
  { id: 'other', run_id: 'one', supersedes: null, questions: ['相同问题'] },
] as Finding[];
describe('调查汇总与逐轮展示', () => {
  it('发现依据只展示绑定资料及其原文，排除冻结池的其他检索命中', () => {
    const evidence = { block: { evidence_ids: ['chunk'], segments: [] }, artifacts: {}, sources: {
      chunk: { revision_id: 'revision', title: '片段', source_kind: 'official_reference', content: '', locator: {} },
      revision: { revision_id: 'revision', title: '原文', source_kind: 'official_reference', content: '', locator: {} },
      unused: { revision_id: 'unused', title: '未引用检索命中', source_kind: 'official_reference', content: '', locator: {} },
    } } as FindingEvidence;
    expect(Object.keys(boundFindingEvidence(evidence).sources)).toEqual(['chunk', 'revision']);
  });
  it('本轮保留被后轮修正的发现，归属不混用', () => {
    const first = findingGroups(findings, 'one');
    expect(first.items.map(f => f.id)).toEqual(['old', 'other']);
    expect(first.supersededBy.get('old')?.run_id).toBe('two');
    expect(first.questions).toEqual([{ text: '旧问题', historical: true }, { text: '相同问题', historical: false }]);
  });
  it('汇总只从当前有效发现提取建议，历史仍可查看', () => {
    const summary = findingGroups(findings);
    expect(summary.items.map(f => f.id)).toEqual(['new', 'other']);
    expect(summary.history.map(f => f.id)).toEqual(['old']);
    expect(summary.questions.map(q => q.text)).toEqual(['新问题', '相同问题']);
  });
});
