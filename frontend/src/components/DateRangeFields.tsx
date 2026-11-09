import { Alert, Input } from 'antd';
import { dateRangeError, dateRangeLabel, snapshotRange } from '../api/data';
import type { DateRange, Snapshot } from '../api/data';

/** 作用：显式选择左闭右开日期范围；输入：快照、范围和回调；输出：日期表单。 */
export function DateRangeFields({ snapshot, value, onChange, disabled = false }: {
  snapshot?: Snapshot; value?: DateRange; onChange: (range: DateRange) => void; disabled?: boolean;
}) {
  const coverage = snapshotRange(snapshot);
  const error = dateRangeError(snapshot, value);
  return <div className="date-range-fields">
    <label>开始日期（含）<Input type="date" aria-label="开始日期（含）" value={value?.start ?? ''} min={coverage?.start} max={coverage?.end_exclusive} disabled={disabled} onChange={e => onChange({ start: e.target.value, end_exclusive: value?.end_exclusive ?? '' })} /></label>
    <label>结束日期（不含）<Input type="date" aria-label="结束日期（不含）" value={value?.end_exclusive ?? ''} min={coverage?.start} max={coverage?.end_exclusive} disabled={disabled} onChange={e => onChange({ start: value?.start ?? '', end_exclusive: e.target.value })} /></label>
    <p>可选覆盖：{dateRangeLabel(coverage)}。结束日期当天不计入，例如分析一至三月应选结束日期 4 月 1 日。</p>
    {error && <Alert type="warning" showIcon message={error} />}
  </div>;
}
