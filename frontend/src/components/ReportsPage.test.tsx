import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { chartNumber, evidenceQuery } from '../api/reports';
import { ReportChart } from './ReportChart';
import type { ReportBlock } from '../api/reports';

describe('报告数值与中文图表', () => {
  it('仅将真实数值转成坐标，缺失值不会成为零', () => {
    expect(chartNumber(null)).toBeNull();
    expect(chartNumber(false)).toBeNull();
    expect(chartNumber('')).toBeNull();
    expect(chartNumber('15')).toBeNull();
    expect(chartNumber({ type: 'date', value: '2026' })).toBeNull();
    expect(chartNumber({ type: 'decimal', value: '15.20' })).toBe(15.2);
    expect(chartNumber(0)).toBe(0);
  });
  it('取证参数只描述报告内的选择，正确保留中文列名', () => {
    const result = new URLSearchParams(evidenceQuery({ block_id: '对比', row: 2, column: '航班数' }));
    expect(result.get('column')).toBe('航班数');
    expect(result.has('artifact_id')).toBe(false);
  });
  it('柱状图保留中文与服务器显示文本，不把空值画成零柱', () => {
    const block: ReportBlock = { id: 'bar', type: 'chart', title: '航空公司对比', kind: 'bar', ordered: false,
      columns: [{ name: 'carrier', label: '航空公司' }, { name: 'count', label: '记录数' }],
      rows: [[{ raw: '合成航空甲', display: '合成航空甲', value_ref: { artifact_id: 'qa_test', row: 0, column: 'carrier' } },
        { raw: null, display: '无可用值', value_ref: { artifact_id: 'qa_test', row: 0, column: 'count' } }]] };
    const html = renderToStaticMarkup(<ReportChart block={block} inspect={() => {}} />);
    expect(html).toContain('没有按零绘图');
    expect(html).not.toContain('bar-fill');
    block.rows![0][1] = { raw: 15.2, display: '15.20 分钟', value_ref: { artifact_id: 'qa_test', row: 0, column: 'count' } };
    const actual = renderToStaticMarkup(<ReportChart block={block} inspect={() => {}} />);
    expect(actual).toContain('合成航空甲');
    expect(actual).toContain('15.20 分钟');
    expect(actual).not.toContain('�');
  });
});
