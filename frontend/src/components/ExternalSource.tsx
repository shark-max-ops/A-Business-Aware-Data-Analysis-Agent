import { useState } from 'react';
import { Button, Tag } from 'antd';

type Source = { title: string; content: string; source_kind: string; locator?: Record<string, unknown> };
type Coverage = { unit?: string; accepted_observation_times?: number; hours_without_observation?: number; expected_utc_hours?: number; exact_min?: number | null; exact_max?: number | null };
const fieldNames: Record<string, string> = { temperature: '温度', wind_speed: '风速', visibility: '能见度', precipitation: '降水', snow_depth: '积雪' };

/** 作用：缩短 UTC 时间展示；输入：原始时间；输出：易读时间，未知不猜。 */
function utcText(value: unknown): string {
  if (typeof value !== 'string') return '未知';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toISOString().slice(0, 16).replace('T', ' ');
}

/** 作用：限制展示小数长度；输入：固定原值；输出：至多两位小数，原值不变。 */
function numberText(value: number | null | undefined): string {
  return value == null ? '缺失' : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

/** 作用：识别固定外部来源；输入：来源类型；输出：是否使用外部证据卡片。 */
export function isExternalSource(kind: string): boolean {
  return kind === 'official_weather_observations' || kind === 'official_advisory';
}

/** 作用：把固定天气覆盖或公告原文呈现为可读依据；输入：服务端冻结来源；输出：中文来源卡。 */
export function ExternalSource({ source: incoming }: { source: Source }) {
  const source = { ...incoming, locator: incoming.locator ?? {} };
  const [visible, setVisible] = useState(20);
  const weather = source.source_kind === 'official_weather_observations';
  let data: { mapping?: { airport?: string; station?: string; timezone?: string }; coverage?: Record<string, Coverage>; observations?: { observed_at: string; locator?: unknown; fields: Record<string, { value?: number | null; unit?: string; accepted?: boolean; qualifier?: string }> }[] } = {};
  if (weather) {
    try { data = JSON.parse(source.content.slice(source.content.indexOf('{'))); } catch { /* 旧来源无法解析时保留固定原文入口。 */ }
  }
  const fetches = Array.isArray(source.locator.fetches) ? source.locator.fetches as { url?: string; fetched_at?: string; params?: Record<string, string> }[] : [];
  const effective = source.locator.effective_time as { start_utc?: string; end_utc?: string } | null;
  return <details className="external-source">
    <summary><Tag>{weather ? '官方天气观测' : '官方运行公告'}</Tag><span>{source.title}</span></summary>
    <div className="external-source-body">
      {weather ? <>
        {(source.locator.acquisition as { cache_used?: boolean } | undefined)?.cache_used && <p className="external-caption">本次复用已核验的历史原件；下方保留原获取时间，并非本轮重新联网取得。</p>}
        {Array.isArray((source.locator.acquisition as { revalidations?: unknown[] } | undefined)?.revalidations) && (source.locator.acquisition as { revalidations: unknown[] }).revalidations.length > 0 && <p className="external-caption">本轮已联网向官方核对版本，官方确认原件未变（HTTP 304）；复用原件，未重新下载全年文件。</p>}
        <p className="external-caption">{data.mapping?.airport} · 气象站 {data.mapping?.station} · {data.mapping?.timezone}</p>
        <p>观测范围（UTC）：{utcText(source.locator.utc_start)} 至 {utcText(source.locator.utc_end_exclusive)}，右端不含。</p>
        <div className="external-coverage">{Object.entries(data.coverage ?? {}).map(([field, coverage]) => <article key={field}>
          <strong>{fieldNames[field] ?? field}</strong><span>{coverage.accepted_observation_times ?? '未知'} 个可用观测时刻</span>
          <small>无合格观测：{coverage.hours_without_observation ?? '未知'}/{coverage.expected_utc_hours ?? '未知'} 个 UTC 小时</small>
          {coverage.exact_min != null && <small>精确观测范围：{numberText(coverage.exact_min)} 至 {numberText(coverage.exact_max)} {coverage.unit}</small>}
        </article>)}</div>
        <p className="external-caption">每小时有观测不代表连续覆盖。缺失不补零，界限值不作为精确值，降水和积雪未累计；观测不能单独证明航班原因。</p>
        {data.observations && <details><summary>查看固定观测明细 · {data.observations.length} 条原始记录</summary>
          <div className="external-observations">{data.observations.slice(0, visible).map((observation, index) => <article key={index}>
            <time>{utcText(observation.observed_at)}（UTC）</time>
            <p>{Object.entries(observation.fields).map(([name, cell]) => `${fieldNames[name] ?? name}：${cell.value == null ? '缺失' : `${cell.qualifier ?? ''}${numberText(cell.value)} ${cell.unit ?? ''}`}${cell.value != null && !cell.accepted ? '（未通过筛选）' : ''}`).join(' · ')}</p>
          </article>)}</div>
          <p>显示 {Math.min(visible, data.observations.length)}/{data.observations.length} 条；预览不代替完整统计。</p>
          {visible < data.observations.length && <Button size="small" onClick={() => setVisible(visible + 20)}>再看 20 条</Button>}
        </details>}
      </> : <>
        <dl className="external-times"><div><dt>签发时间（UTC）</dt><dd>{utcText(source.locator.published_at)}</dd></div><div><dt>原文有效期（UTC）</dt><dd>{effective ? `${utcText(effective.start_utc)} 至 ${utcText(effective.end_utc)}` : '未能确定，请核对固定原文'}</dd></div></dl>
        <p className="external-caption">请核对原文中的机场、程序时段和航司限制。管制取消不等于航班取消，预计延误不是实际统计。</p>
        <details><summary>阅读固定公告原文 · 带行号</summary><div className="external-original">{source.content.split('\n').map((line, index) => <div key={index}><span>{index + 1}</span><pre>{line || ' '}</pre></div>)}</div></details>
      </>}
      {fetches.map((fetch, index) => <p className="external-caption" key={index}>获取时间：{utcText(fetch.fetched_at)}（UTC）{fetch.url && /^https:\/\/(aviationweather\.gov|www\.ncei\.noaa\.gov|www\.fly\.faa\.gov)\//.test(fetch.url) && <> · <a href={fetch.url + (fetch.params && Object.keys(fetch.params).length ? `?${new URLSearchParams(fetch.params)}` : '')} target="_blank" rel="noreferrer">官方入口 ↗</a>（在线内容可能更新）</>}</p>)}
      {weather && <p className="external-caption">展示最多保留两位小数，固定原值保留原始精度。</p>}
      <details className="technical-details"><summary>来源定位与固定版本</summary><pre>{JSON.stringify(source.locator, null, 2)}</pre>{weather && <details><summary>完整固定观测与原始精度</summary><pre>{source.content}</pre></details>}</details>
    </div>
  </details>;
}
