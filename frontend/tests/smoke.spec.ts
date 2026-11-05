import { test, expect } from '@playwright/test';

test('real backend, navigation and disabled analysis', async ({ page, request }) => {
  const sources = await (await request.get('/api/v1/sources')).json();
  const synthetic = sources.items.find((source: { source_kind: string }) => source.source_kind === 'synthetic');
  expect(synthetic).toBeDefined();
  await page.goto('/analysis');
  await expect(page.getByText('本地服务已连接', { exact: false })).toBeVisible();
  const health = await (await request.get('/api/v1/health')).json();
  await expect(page.getByText(health.llm === 'configured_unverified' ? '模型配置已填写' : '模型未配置', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送问题' })).toBeDisabled();
  await page.getByRole('button', { name: '查看数据源' }).click();
  await page.getByRole('combobox', { name: '数据源', exact: true }).press('ArrowDown');
  await page.locator('.ant-select-item-option-content').getByText(synthetic.name, { exact: true }).click();
  await expect(page.getByText('人工合成测试数据', { exact: true })).toBeVisible();
  await expect(page.getByText('v_flights', { exact: true })).toBeVisible();
  await expect(page.getByText('arrival_delay', { exact: true })).toBeVisible();
  await page.getByRole('tab', { name: '质量报告' }).click();
  await expect(page.getByText('out_of_period', { exact: false })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('link', { name: '知识管理', exact: true }).click();
  await expect(page.getByRole('button', { name: '＋ 新建文档', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '让知识，成为分析的起点。' })).toBeVisible();
  await page.getByRole('link', { name: '报告', exact: true }).click();
  await expect(page.getByRole('heading', { name: '每个数字，都能找到依据' })).toBeVisible();
  await page.screenshot({ path: '../.cache/screenshots/reports-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: '../.cache/screenshots/reports-mobile.png', fullPage: true });
  await page.goto('/analysis');
  await page.screenshot({ path: '../.cache/screenshots/analysis-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({ path: '../.cache/screenshots/analysis-desktop.png', fullPage: true });
});

test('backend failure and recovery', async ({ page }) => {
  await page.route('**/api/v1/health', route => route.abort());
  await page.goto('/analysis');
  await expect(page.getByText('后端连接失败', { exact: true })).toBeVisible();
  await page.unroute('**/api/v1/health');
  await page.getByRole('button', { name: '重新连接' }).first().click();
  await expect(page.getByText('本地服务已连接', { exact: false })).toBeVisible();
});


test('registered real BTS snapshot, schema and quality', async ({ page, request }) => {
  const sources = await (await request.get('/api/v1/sources')).json();
  const source = sources.items.find((item: { source_kind: string; current_snapshot_id: string | null }) => item.source_kind === 'official_bts' && item.current_snapshot_id);
  test.skip(!source, '真实 CSV 尚未在本地登记');
  const snapshots = await (await request.get(`/api/v1/sources/${source.id}/snapshots`)).json();
  const snapshot = snapshots.items.find((item: { id: string }) => item.id === source.current_snapshot_id);
  await page.goto('/analysis');
  await page.getByRole('button', { name: '查看数据源' }).click();
  await page.getByRole('combobox', { name: '数据源', exact: true }).press('ArrowDown');
  await page.locator('.ant-select-item-option-content').getByText(source.name, { exact: true }).click();
  await expect(page.getByText('用户登记的 BTS Reporting 来源', { exact: true })).toBeVisible();
  await expect(page.getByText(snapshot.row_count.toLocaleString(), { exact: true })).toBeVisible();
  await expect(page.getByText('v_flights', { exact: true })).toBeVisible();
  await expect(page.getByText('arrival_delay', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.cache/screenshots/real-bts-source.png', fullPage: true });
  await page.getByRole('tab', { name: '质量报告' }).click();
  await expect(page.getByText('negative_duration_counts', { exact: false })).toBeVisible();
  await page.screenshot({ path: '../.cache/screenshots/real-bts-quality.png', fullPage: true });
});
