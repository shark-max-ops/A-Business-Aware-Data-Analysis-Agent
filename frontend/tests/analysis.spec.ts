import { test, expect } from '@playwright/test';

test('real runtime, repair, reload, event replay and mobile layout', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/analysis');
  await expect(page.getByText('本地服务已连接', { exact: false })).toBeVisible();
  await page.getByLabel('分析问题', { exact: true }).fill('核对合成数据范围');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.getByRole('button', { name: '停止分析' })).toBeVisible();
  await page.reload();
  await expect(page.getByText('查询已完成。以下是冻结结果预览', { exact: false })).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('columnheader', { name: 'record_count' })).toBeVisible();
  await expect(page.getByText('查询未成功，未生成证据产物', { exact: true })).toBeVisible();
  await expect(page.getByText('查询完成，保存 1 行', { exact: true })).toBeVisible();
  const count = await page.locator('.run-event').count();
  await page.reload();
  await expect(page.locator('.run-event')).toHaveCount(count);
  await expect(page.getByText('查询完成，保存 1 行', { exact: true })).toHaveCount(1);
  await page.screenshot({ path: '../.cache/screenshots/stage3-analysis-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: '../.cache/screenshots/stage3-analysis-mobile.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('explicit cancellation survives reload and stops model work', async ({ page, request }) => {
  await page.goto('/analysis');
  await page.getByLabel('分析问题', { exact: true }).fill('等待取消');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page.getByRole('button', { name: '停止分析' })).toBeVisible();
  await page.reload();
  const stop = page.getByRole('button', { name: '停止分析' });
  await expect(stop).toBeVisible();
  await stop.click();
  await expect(page.getByText('已取消：RUN_CANCELLED', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('已取消：RUN_CANCELLED', { exact: true })).toBeVisible();
  const sessions = await (await request.get('/api/v1/conversations')).json();
  const runs = await (await request.get(`/api/v1/conversations/${sessions.items[0].id}`)).json();
  expect(runs.items.at(-1).status).toBe('cancelled');
});
