import { useId, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Empty, Select } from 'antd';
import { chartNumber } from '../api/reports';
import type { ReportBlock, Selection } from '../api/reports';

const colors = ['var(--chart-accent)', 'var(--chart-compare)', 'var(--chart-compare-light)'];
const comparison = /历史|同期|去年|上年|上月|上期|前期|对照|baseline|prior|previous/i;

/** 作用：只从冻结数据绘制白名单图表；输入：已物化块和点击处理；输出：中文图表及可访问的数据入口。 */
export function ReportChart({ block, inspect, evidence }: { block: ReportBlock; inspect: (selection: Selection) => void; evidence?: ReactNode }) {
  const titleId = useId();
  const frame = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const [metric, setMetric] = useState<number | null>(null);
  useLayoutEffect(() => {
    const node = frame.current;
    if (!node) return;
    const measure = () => setWidth(Math.max(260, node.clientWidth));
    const observer = new ResizeObserver(measure);
    observer.observe(node); measure();
    return () => observer.disconnect();
  }, [block.id, block.kind, block.rows?.length, metric]);
  const rows = block.rows ?? [];
  const columns = block.columns ?? [];
  const primary = columns.slice(1).findIndex(column => !comparison.test(column.label));
  let comparisonIndex = 0;
  const metrics = columns.slice(1).map((column, index) => {
    const tone = index === primary ? 0 : 1 + comparisonIndex++ % (colors.length - 1);
    return { ...column, index: index + 1, color: colors[tone], dash: tone === 1 ? '5 4' : tone === 2 ? '2 4' : undefined };
  });
  const series = metric === null ? metrics : metrics.filter(column => column.index === metric);
  const all = rows.flatMap(row => series.map(column => chartNumber(row[column.index]?.raw))).filter((value): value is number => value !== null);
  const low = Math.min(0, ...all);
  const high = Math.max(0, ...all);
  const span = high - low || 1;
  const chartRows = <details className="chart-data"><summary>查看图表数据与依据</summary><div className="chart-data-scroll"><table><thead><tr>{columns.map((column, index) => <th key={column.name} className={index > 0 ? 'numeric-cell' : undefined}>{column.label}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((value, columnIndex) => <td className={columnIndex > 0 ? 'numeric-cell' : undefined} key={columnIndex}><button className="evidence-value" onClick={() => inspect({ block_id: block.id, row: rowIndex, column: columns[columnIndex].name })}>{value.display}</button></td>)}</tr>)}</tbody></table></div></details>;
  return <div className="report-chart">
    <div className="report-block-heading"><h2>{block.title}</h2><div className="report-chart-actions">
      {rows.length > 0 && metrics.length > 1 && <Select size="small" aria-label="图表指标" value={metric ?? 0}
        options={[{ label: '全部指标', value: 0 }, ...metrics.map(column => ({ label: column.label, value: column.index }))]}
        onChange={value => setMetric(value || null)} />}
      {evidence}
    </div></div>
    {all.length > 0 && <div className="chart-legend">{series.map(column => <span key={column.name}><i style={{ background: column.color }} />{column.label}</span>)}</div>}
    {!rows.length ? <Empty description="这个范围没有查询结果" /> : !all.length ? <Empty description="这些数值均缺失，没有按零绘图" /> :
    block.kind === 'bar' ? <div className="bar-chart">{rows.map((row, rowIndex) => <div className="bar-category" key={rowIndex}><p>{row[0].display.replace(/T00:00:00$/, '')}</p><div>{series.map(column => {
      const value = row[column.index];
      const number = chartNumber(value.raw);
      return <button className="bar-point" key={column.name} aria-label={`${row[0].display}，${column.label}：${value.display}，查看依据`} onClick={() => inspect({ block_id: block.id, row: rowIndex, column: column.name })}>
        <span className="bar-track"><i className="bar-zero" style={{ left: `${-low / span * 100}%` }} />{number !== null && number !== 0 && <i className="bar-fill" style={{ left: `${(Math.min(0, number) - low) / span * 100}%`, width: `${Math.abs(number) / span * 100}%`, background: column.color }} />}</span><span className="bar-value">{value.display}</span>
      </button>;
    })}</div></div>)}</div> : <div className="line-chart-scroll" ref={frame}><svg className="line-chart" viewBox={`0 0 ${width} 214`} role="img" aria-labelledby={titleId}>
      <title id={titleId}>{block.title}，点击数据点查看依据</title>
      <defs>
        <linearGradient id={`${titleId}-area`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--chart-accent)" stopOpacity="0.16" />
          <stop offset="100%" stopColor="var(--chart-accent)" stopOpacity="0.01" />
        </linearGradient>
      </defs>
      {[0, .25, .5, .75, 1].map(fraction => <g key={fraction}><line x1="65" x2={width - 26} y1={160 - fraction * 140} y2={160 - fraction * 140} stroke="var(--chart-grid)" strokeDasharray={fraction === 0 ? undefined : '3 3'} /><text x="54" y={164 - fraction * 140} textAnchor="end" className="chart-tick">{new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2, notation: Math.max(Math.abs(low), Math.abs(high)) >= 100000 ? 'compact' : 'standard' }).format(low + fraction * span)}</text></g>)}
      {series.map((column, seriesIndex) => {
        const xNumbers = rows.map((row, index) => {
          const raw = row[0].raw;
          if (typeof raw === 'object' && raw !== null && 'type' in raw && ['date', 'datetime'].includes(String(raw.type)) && 'value' in raw) return Date.parse(String(raw.value));
          return chartNumber(raw) ?? index;
        });
        const first = xNumbers[0], last = xNumbers.at(-1) ?? first;
        const x = (index: number) => first === last ? width / 2 : 65 + (xNumbers[index] - first) / (last - first) * (width - 91);
        const y = (number: number) => 160 - (number - low) / span * 140;
        let started = false;
        const validPoints: Array<{ x: number; y: number }> = [];
        const path = rows.map((row, index) => {
          const number = chartNumber(row[column.index].raw);
          if (number === null) { started = false; return ''; }
          const px = x(index), py = y(number);
          validPoints.push({ x: px, y: py });
          const part = `${started ? 'L' : 'M'}${px} ${py}`;
          started = true; return part;
        }).join(' ');
        const areaPath = seriesIndex === 0 && validPoints.length > 1 && validPoints.length === rows.length
          ? `${path} L${validPoints[validPoints.length - 1].x} 160 L${validPoints[0].x} 160 Z`
          : '';
        return <g key={column.name}>{areaPath && <path d={areaPath} fill={`url(#${titleId}-area)`} />}<path d={path} fill="none" stroke={column.color} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" strokeDasharray={column.dash} />{rows.map((row, index) => {
          const value = row[column.index], number = chartNumber(value.raw);
          if (number === null) return null;
          return <circle key={index} cx={x(index)} cy={y(number)} r="4.5" fill={column.color} stroke="#fffefa" strokeWidth="1.8" tabIndex={0} role="button" aria-label={`${row[0].display}，${column.label}：${value.display}，查看依据`} onClick={() => inspect({ block_id: block.id, row: index, column: column.name })} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); inspect({ block_id: block.id, row: index, column: column.name }); } }}><title>{row[0].display} · {column.label}：{value.display}</title></circle>;
        })}{seriesIndex === 0 && rows.map((row, index) => (index === 0 || index === rows.length - 1 || (width > 400 && index % Math.ceil(rows.length / 5) === 0)) && <text key={index} x={x(index)} y="183" textAnchor={index === 0 ? 'start' : index === rows.length - 1 ? 'end' : 'middle'} className="chart-tick">{row[0].display.replace(/T00:00:00$/, '')}</text>)}</g>;
      })}
      <text x={width / 2} y="204" textAnchor="middle" className="chart-tick">{columns[0]?.label}</text>
    </svg></div>}
    {rows.length > 0 && chartRows}
  </div>;
}
