import { Alert, Descriptions, Table } from 'antd';
import { dateRangeLabel, snapshotRange } from '../api/data';
import type { Snapshot } from '../api/data';

type Counts = Record<string, number>;
type Quality = { null_counts?: Counts; negative_duration_counts?: Counts; duplicate_candidates?: number; months?: Record<string, Quality>; policy?: string };
type SourceMonth = { period: string; rows?: number; date_min?: string; date_max?: string };
const fieldLabels: Record<string, string> = { scheduled_elapsed: '计划时长', actual_elapsed: '实际时长', air_time: '空中时长', arrival_delay: '到达时间偏差', departure_delay: '出发时间偏差' };

/** 作用：呈现冻结快照的实际覆盖与保留质量；输入：快照；输出：可读摘要。 */
export function SnapshotSummary({ snapshot }: { snapshot: Snapshot }) {
  const quality = snapshot.quality_report as Quality | undefined;
  const coverage = snapshotRange(snapshot);
  const sources = snapshot.manifest?.sources as SourceMonth[] | undefined;
  const periods = snapshot.period ? [snapshot.period] : snapshot.periods ?? [];
  const missing: string[] = [];
  if (coverage) {
    let cursor = coverage.start.slice(0, 7);
    while (`${cursor}-01` < coverage.end_exclusive) {
      if (!periods.includes(cursor)) missing.push(cursor);
      const [year, month] = cursor.split('-').map(Number);
      cursor = `${year + (month === 12 ? 1 : 0)}-${String(month % 12 + 1).padStart(2, '0')}`;
    }
  }
  const counts = (values?: Counts) => values ? Object.entries(values).filter(([, n]) => n > 0).map(([key, n]) => `${fieldLabels[key] ?? key}：${n.toLocaleString()}`).join('；') || '已检查字段为 0' : '未记录';
  return <section aria-label="快照覆盖与质量摘要">
    <Descriptions size="small" column={1} items={[
      { key: 'coverage', label: '可选日期覆盖', children: dateRangeLabel(coverage) },
      { key: 'gaps', label: '缺月情况', children: missing.length ? missing.join('、') : coverage ? '覆盖内无缺月；不代表逐日无缺测' : '未记录' },
      { key: 'negative', label: '负时长原值', children: counts(quality?.negative_duration_counts) },
      { key: 'nulls', label: '字段空值数', children: counts(quality?.null_counts) },
      { key: 'duplicates', label: '重复候选', children: quality?.duplicate_candidates?.toLocaleString() ?? '未记录' },
    ]} />
    {missing.length > 0 && <Alert type="warning" message="不能创建跨越缺月的调查，请缩小范围" />}
    {sources?.length ? <Table size="small" pagination={false} rowKey="period" scroll={{ x: 440 }} dataSource={sources} columns={[
      { title: '来源月份', dataIndex: 'period' },
      { title: '实际记录日期', render: (_, item) => item.date_min && item.date_max ? `${item.date_min} 至 ${item.date_max}（含）` : '未记录' },
      { title: '全月记录数', render: (_, item) => item.rows?.toLocaleString() ?? '未记录' },
      { title: '负计划时长', render: (_, item) => quality?.months?.[item.period]?.negative_duration_counts?.scheduled_elapsed?.toLocaleString() ?? '未记录' },
    ]} /> : null}
    <p>{quality?.policy ?? '旧快照未记录质量处理说明，请查看原始质量报告。'} 以上统计属于完整快照，所选调查范围内数量由实际查询确定。</p>
  </section>;
}
