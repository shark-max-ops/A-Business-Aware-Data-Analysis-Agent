import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ReadableMarkdown } from './ReadableMarkdown';
import { MetricSummary } from './MetricSummary';
import { readableFilter, readableRule } from './knowledgePresentation';
import type { MetricDefinition } from '../api/knowledge';

describe('knowledge reading views', () => {
  it('renders Markdown tables and blocks active HTML, images and unsafe links', () => {
    const html = renderToStaticMarkup(<ReadableMarkdown text={'| 指标 | 含义 |\n| --- | --- |\n| ArrDelay | 保留负数 |\n\n<script>alert(1)</script>\n\n[危险](javascript:alert(1))\n\n![图片](https://example.org/tracker)'} />);
    expect(html).toContain('<table>'); expect(html).toContain('<th>指标</th>'); expect(html).toContain('<td>保留负数</td>');
    expect(html).not.toContain('<script'); expect(html).not.toContain('javascript:'); expect(html).not.toContain('<img');
  });
  it('shows human citation numbers without exposing machine IDs', () => {
    const html = renderToStaticMarkup(<ReadableMarkdown text="提前到达为负数。[chunk_a123]" citations={[{ chunk_id: 'chunk_a123', excerpt: '提前到达为负数', text_hash: 'test' }]} />);
    expect(html).toContain('查看引用 1'); expect(html).not.toContain('chunk_a123');
    const table = renderToStaticMarkup(<ReadableMarkdown text={'| 字段 | 含义 |\n| --- | --- |\n| ArrDelay | 提前到达为负数 | [chunk_a123]'} citations={[{ chunk_id: 'chunk_a123', excerpt: '提前到达为负数', text_hash: 'test' }]} />);
    expect(table).toContain('<table>'); expect(table).toContain('查看引用 1');
  });
  it('translates only supported rules and preserves unfamiliar conditions', () => {
    expect(readableFilter('cancelled = 0')).toBe('未取消');
    expect(readableRule('E 中 arrival_delay 的总和')).toBe('有效到达航班中到达时间偏差的总和');
    expect(readableFilter('custom_flag = 7')).toBe('custom_flag = 7');
  });
  it('explains numerator, denominator, missingness and authority before the edit form', () => {
    const d: MetricDefinition = { key: 'test_rate', version: 1, name: '有效到达延误率', authority: 'project_policy', review_state: 'draft', unit: 'percent', grain: 'carrier_id', numerator_description: 'E 中到达延误记录数 × 100', denominator_description: 'E 的记录数', population_filters: ['cancelled=0', 'diverted=0'], required_columns: ['arrival_delay'], null_policy: '零分母 null', effective_from: '2026-01-01', effective_to: '2026-02-01', source_revision_ids: [], template_key: null };
    const html = renderToStaticMarkup(<MetricSummary definition={d} documents={[]} />);
    for (const label of ['分子', '分母', '未取消', '未备降', '零分母 null', '项目自定义', '待审核草稿']) expect(html).toContain(label);
    expect(html).not.toContain('<input');
  });
});
