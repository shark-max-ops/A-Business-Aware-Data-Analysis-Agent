import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { ExternalSource } from './ExternalSource';

it('distinguishes a verified cached original from a fresh fetch and preserves acquisition time', () => {
  const source = { title: '合成历史天气来源', source_kind: 'official_weather_observations',
    content: '合成资料\n' + JSON.stringify({ mapping: { airport: 'ATL', station: 'synthetic', timezone: 'America/New_York' }, coverage: {} }),
    locator: { acquisition: { cache_used: true }, utc_start: '2026-01-01T05:00:00Z', utc_end_exclusive: '2026-01-02T05:00:00Z',
      fetches: [{ fetched_at: '2026-01-01T06:15:00Z' }] } };
  const cached = renderToStaticMarkup(<ExternalSource source={source} />);
  expect(cached).toContain('并非本轮重新联网取得');
  expect(cached).toContain('2026-01-01 06:15');
  const fresh = renderToStaticMarkup(<ExternalSource source={{ ...source, locator: { ...source.locator, acquisition: { cache_used: false } } }} />);
  expect(fresh).not.toContain('并非本轮重新联网取得');
  const checked = renderToStaticMarkup(<ExternalSource source={{ ...source, locator: { ...source.locator, acquisition: { cache_used: true, revalidations: [{ http_status: 304 }] } } }} />);
  expect(checked).toContain('官方确认原件未变（HTTP 304）');
  expect(checked).toContain('2026-01-01 06:15');
});
