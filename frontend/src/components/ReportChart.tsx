import { useId, useLayoutEffect, useRef, useState } from 'react';
import { Empty } from 'antd';
import { chartNumber } from '../api/reports';
import type { ReportBlock, Selection } from '../api/reports';

const colors = ['#28765f', '#6488b2', '#bf934e'];

/** 作用：只从冻结数据绘制白名单图表；输入：已物化块和点击处理；输出：中文图表及可访问的数据入口。 */
export function ReportChart({ block, inspect }: { block: ReportBlock; inspect: (selection: Selection) => void }) {
  const titleId = useId();
  const frame = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  useLayoutEffect(() => {
    const node = frame.current;
    if (!node) return;
    const measure = () => setWidth(Math.max(260, node.clientWidth));
    const observer = new ResizeObserver(measure);
    observer.observe(node); measure();
    return () => observer.disconnect();
  }, [block.id, block.kind, block.rows?.length]);
  const rows = block.rows ?? [];
  const columns = block.columns ?? [];
  if (!rows.length) return <Empty description="这个范围没有查询结果" />;
  const series = columns.slice(1);
  const all = rows.flatMap(row => row.slice(1).map(value => chartNumber(value.raw))).filter((value): value is number => value !== null);
  const low = Math.min(0, ...all);
  const high = Math.max(0, ...all);
  const span = high - low || 1;
  const chartRows = <details className="chart-data"><summary>查看图表数据与依据</summary><div className="chart-data-scroll"><table><thead><tr>{columns.map(column => <th key={column.name}>{column.label}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((value, columnIndex) => <td key={columnIndex}><button className="evidence-value" onClick={() => inspect({ block_id: block.id, row: rowIndex, column: columns[columnIndex].name })}>{value.display}</button></td>)}</tr>)}</tbody></table></div></details>;
  if (!all.length) return <div className="report-chart"><Empty description="这些数值均缺失，没有按零绘图" />{chartRows}</div>;
  return <div className="report-chart">
    <div className="chart-legend">{series.map((column, i) => <span key={column.name}><i style={{ background: colors[i] }} />{column.label}</span>)}</div>
    {block.kind === 'bar' ? <div className="bar-chart">{rows.map((row, rowIndex) => <div className="bar-category" key={rowIndex}><p>{row[0].display}</p><div>{row.slice(1).map((value, i) => {
      const number = chartNumber(value.raw);
      return <button className="bar-point" key={i} aria-label={`${row[0].display}，${series[i].label}：${value.display}，查看依据`} onClick={() => inspect({ block_id: block.id, row: rowIndex, column: series[i].name })}>
        <span className="bar-track"><i className="bar-zero" style={{ left: `${-low / span * 100}%` }} />{number !== null && number !== 0 && <i className="bar-fill" style={{ left: `${(Math.min(0, number) - low) / span * 100}%`, width: `${Math.abs(number) / span * 100}%`, background: colors[i] }} />}</span><span className="bar-value">{value.display}</span>
      </button>;
    })}</div></div>)}</div> : <div className="line-chart-scroll" ref={frame}><svg className="line-chart" viewBox={`0 0 ${width} 320`} role="img" aria-labelledby={titleId}>
      <title id={titleId}>{block.title}，点击数据点查看依据</title>
      {[0, .25, .5, .75, 1].map(fraction => <g key={fraction}><line x1="65" x2={width - 30} y1={260 - fraction * 220} y2={260 - fraction * 220} stroke="#e6ece7" /><text x="54" y={265 - fraction * 220} textAnchor="end" className="chart-tick">{new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2, notation: Math.max(Math.abs(low), Math.abs(high)) >= 100000 ? 'compact' : 'standard' }).format(low + fraction * span)}</text></g>)}
      {series.map((column, seriesIndex) => {
        const xNumbers = rows.map((row, index) => {
          const raw = row[0].raw;
          if (typeof raw === 'object' && raw !== null && 'type' in raw && ['date', 'datetime'].includes(String(raw.type)) && 'value' in raw) return Date.parse(String(raw.value));
          return chartNumber(raw) ?? index;
        });
        const first = xNumbers[0], last = xNumbers.at(-1) ?? first;
        const x = (index: number) => first === last ? width / 2 : 65 + (xNumbers[index] - first) / (last - first) * (width - 95);
        const y = (number: number) => 260 - (number - low) / span * 220;
        let started = false;
        const path = rows.map((row, index) => {
          const number = chartNumber(row[seriesIndex + 1].raw);
          if (number === null) { started = false; return ''; }
          const part = `${started ? 'L' : 'M'}${x(index)} ${y(number)}`;
          started = true; return part;
        }).join(' ');
        return <g key={column.name}><path d={path} fill="none" stroke={colors[seriesIndex]} strokeWidth="2.5" />{rows.map((row, index) => {
          const value = row[seriesIndex + 1], number = chartNumber(value.raw);
          if (number === null) return null;
          return <circle key={index} cx={x(index)} cy={y(number)} r="5" fill={colors[seriesIndex]} tabIndex={0} role="button" aria-label={`${row[0].display}，${column.label}：${value.display}，查看依据`} onClick={() => inspect({ block_id: block.id, row: index, column: column.name })} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); inspect({ block_id: block.id, row: index, column: column.name }); } }}><title>{row[0].display} · {column.label}：{value.display}</title></circle>;
        })}{seriesIndex === 0 && rows.map((row, index) => (index === 0 || index === rows.length - 1 || (width > 550 && index % Math.ceil(rows.length / 5) === 0)) && <text key={index} x={x(index)} y="288" textAnchor={index === 0 ? 'start' : index === rows.length - 1 ? 'end' : 'middle'} className="chart-tick">{row[0].display}</text>)}</g>;
      })}
      <text x={width / 2} y="313" textAnchor="middle" className="chart-tick">{columns[0]?.label}</text>
    </svg></div>}
    {all.length === 0 && <p className="report-note">这些数值均缺失；图中没有将缺失值当作零。</p>}
    {chartRows}
  </div>;
}
