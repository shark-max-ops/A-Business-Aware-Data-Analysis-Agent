import { test, expect } from '@playwright/test';

test('real backend, navigation and disabled analysis', async ({ page }) => {
  await page.goto('/analysis');
  await expect(page.getByText('本地服务已连接', { exact: false })).toBeVisible();
  await expect(page.getByText('模型未配置', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '发送问题' })).toBeDisabled();
  await page.getByRole('button', { name: '查看数据源' }).click();
  await expect(page.getByText('数据导入尚未开放', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('link', { name: '知识管理', exact: true }).click();
  await expect(page.getByText('文档导入尚未开放', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '为每个指标建立依据' })).toBeVisible();
  await page.getByRole('link', { name: '报告', exact: true }).click();
  await expect(page.getByText('报告功能尚未开放', { exact: true })).toBeVisible();
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
  await page.getByRole('button', { name: '重新连接' }).click();
  await expect(page.getByText('本地服务已连接', { exact: false })).toBeVisible();
});
