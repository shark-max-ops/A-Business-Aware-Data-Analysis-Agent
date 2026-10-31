import { Button, Tag } from 'antd';
import type { DocumentItem, MetricDefinition } from '../api/knowledge';
import { fieldLabels, readableFilter, readableRule } from './knowledgePresentation';

/** 作用：先解释计算含义，再提供原始计算细节；输入：真实指标定义及文档；输出：指标阅读卡片。 */
export function MetricSummary({ definition: d, documents, openSource }: { definition: MetricDefinition; documents: DocumentItem[]; openSource?: (id: string) => void }) {
  const percent = d.unit === 'percent';
  return <section className="metric-summary" aria-label="指标口径概览">
    <div className="metric-summary-title"><div><p className="eyebrow">计算口径</p><h2>{d.name}</h2><p>{d.unit === 'count' ? '回答所选范围内有多少条航班记录。' : percent ? '用明确的分子和分母计算比例，显示为百分比。' : '在明确的航班范围内求平均值，显示为分钟。'}</p></div><Tag color={d.review_state === 'approved' ? 'green' : 'gold'}>{d.review_state === 'approved' ? '已审核' : '待审核草稿'} · v{d.version}</Tag></div>
    <div className="metric-formula" aria-label="计算公式"><div className="metric-fraction"><div><small>{d.denominator_description ? '分子：哪些值相加 / 哪些航班计数' : '统计对象'}</small><strong>{readableRule(d.numerator_description)}</strong></div>{d.denominator_description && <div><small>分母：用哪一批航班 / 哪些分钟作比较</small><strong>{readableRule(d.denominator_description)}</strong></div>}</div><span className="metric-unit">结果单位<br /><strong>{percent ? '%' : d.unit === 'minutes' ? '分钟' : '班 / 条记录'}</strong></span></div>
    <div className="metric-explanation-grid"><article><h3>统计范围</h3><ul>{d.population_filters.map((filter, i) => <li key={i}>{readableFilter(filter)}</li>)}</ul></article><article><h3>没有数据时怎么办</h3><p>{readableRule(d.null_policy)}</p><h3>什么时候适用</h3><p>{d.effective_from} 起，至 {d.effective_to ?? '未声明截止日期'}{d.effective_to ? '（截止当天不计入）' : ''}</p></article></div>
    <div className="metric-sources"><h3>这是谁制定的，依据在哪里</h3><p>{d.authority === 'project_policy' ? '这是项目自定义的计算口径；引用官方字段含义，不把整套公式称为官方指标。' : '官方指标映射，具体定义以所绑定资料为准。'}</p>{d.source_revision_ids.map((id, i) => { const doc = documents.find(doc => doc.current_revision_id === id); return <Button key={id} size="small" onClick={() => openSource?.(id)}>{doc ? doc.title : `来源资料 ${i + 1}（固定历史版本）`}</Button>; })}</div>
    <details className="technical-details"><summary>展开字段与原始计算细节</summary><p>分组：{readableRule(d.grain)}</p><p>所需字段：{d.required_columns.map(column => `${fieldLabels[column] ?? column}（${column}）`).join('；')}</p><p>原始分子：{d.numerator_description}</p><p>原始分母：{d.denominator_description ?? '不适用'}</p><pre>{d.population_filters.join('\n')}</pre></details>
  </section>;
}
