import { test, expect } from '@playwright/test';
import { readFileSync, mkdirSync } from 'node:fs';
import { join, resolve } from 'node:path';

const directory = process.env.STAGE7_DIR ? resolve(process.env.STAGE7_DIR) : '';
test.skip(!directory, '需指定阶段7真实验收目录并显式启动隔离服务');

for (const name of ['ranking', 'comparison', 'production-ranking']) {
  test(`checked relation ${name} is server generated and its proof is visible`, async ({ page }) => {
    test.skip(!process.env.CHECKED_RELATIONS_DIR, '需指定关系核对真实验收目录');
    test.skip(name === 'production-ranking' && !process.env.CHECKED_RELATIONS_PRODUCTION, '正式试用报告须显式选择只读检查');
    const root = resolve(process.env.CHECKED_RELATIONS_DIR!);
    const record = JSON.parse(readFileSync(join(root, `${name}.json`), 'utf8'));
    const report = record.report;
    expect(report.verification_policy).toBe('checked_relations_v1');
    const checked = report.blocks.filter((b: { verification?: string }) => b.verification === 'checked_relation');
    expect(checked.length).toBeGreaterThan(0);
    await page.goto(`/reports/${report.report_id}/revisions/${report.revision_id}`);
    await expect(page.getByRole('heading', { name: report.title, exact: true })).toBeVisible();
    await expect(page.getByText('分析主题（模型拟定）')).toBeVisible();
    if (name.includes('ranking')) {
      const top = checked[0].segments.filter((s: { value?: { value_ref: { column: string }; raw: string } }) => s.value?.value_ref.column === 'carrier_code');
      expect(top.map((s: { value: { raw: string } }) => s.value.raw)).toEqual(['OH', 'NK', 'AA']);
      const low = checked[1].segments.find((s: { value?: { value_ref: { column: string } } }) => s.value?.value_ref.column === 'carrier_code');
      expect(low.value.raw).toBe('B6');
      await expect(page.locator('.report-unverified')).toHaveCount(0);
    } else {
      expect(checked.map((b: { proof: { relation: string } }) => b.proof.relation)).toEqual(['higher', 'lower']);
      await expect(page.locator('.report-unverified')).toHaveJSProperty('open', false);
    }
    await page.getByRole('button', { name: '查看核对依据', exact: true }).first().click();
    const drawer = page.getByRole('dialog', { name: '这个结果从哪里来', exact: true });
    await expect(drawer.getByRole('heading', { name: '服务端关系核对' })).toBeVisible();
    await drawer.getByText('比较范围、空值、并列与核对摘要', { exact: true }).click();
    await expect(drawer).toContainText(checked[0].proof.proof_hash);
    await page.keyboard.press('Escape');
    mkdirSync(join(root, 'screenshots'), { recursive: true });
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: join(root, 'screenshots', `${name}-${viewport.width}.png`), animations: 'disabled' });
    }
  });
}

for (const name of ['monthly-retry', 'carrier', 'causes']) {
  test(`structured finding input ${name} saves once without tool failures`, async ({ page }) => {
    test.skip(!process.env.FINDING_INPUT_DIR, '需指定发现输入修复的独立验收目录');
    const root = resolve(process.env.FINDING_INPUT_DIR!);
    const record = JSON.parse(readFileSync(join(root, `${name}.json`), 'utf8'));
    expect(record.run.status).toBe('completed');
    expect(record.findings).toHaveLength(1);
    await page.addInitScript(id => localStorage.setItem('analysis-conversation', id), record.run.conversation_id);
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto('/analysis');
    const turn = page.locator(`#turn-${record.run.id}`);
    await turn.scrollIntoViewIfNeeded();
    await turn.locator('.execution-toggle').click();
    const step = turn.getByRole('button', { name: /保存发现与依据/ });
    await expect(step).toHaveCount(1);
    await expect(step).toHaveClass(/succeeded/);
    await expect(turn.locator('.execution-step.failed')).toHaveCount(0);
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport); await step.scrollIntoViewIfNeeded();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      mkdirSync(join(root, 'screenshots'), { recursive: true });
      await page.screenshot({ path: join(root, 'screenshots', `${name}-finding-${viewport.width}.png`), animations: 'disabled' });
    }
    await step.click();
    const drawer = page.getByRole('dialog', { name: '执行详情', exact: true });
    await expect(drawer).toContainText('observations');
    await expect(drawer).not.toContainText('本次工具未成功');
    expect(errors).toEqual([]);
  });
}

test('user trial fix shows the verified full table and evidence on desktop and mobile', async ({ page }) => {
  test.skip(!process.env.STAGE7_FIX_DIR, '需指定用户问题修复验收目录');
  const root = resolve(process.env.STAGE7_FIX_DIR!);
  const record = JSON.parse(readFileSync(join(root, 'user-followup-corrected.json'), 'utf8'));
  expect(record.semantic_review.accepted).toBe(true);
  expect(record.report.partial).toBe(false);
  const report = record.report;
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  const screenshots = join(root, 'screenshots'); mkdirSync(screenshots, { recursive: true });
  await page.goto(`/reports/${report.report_id}/revisions/${report.revision_id}`);
  await expect(page.getByRole('heading', { name: report.title, exact: true })).toBeVisible();
  await expect(page.getByText('固定主范围：2026-01-01 至 2026-05-01（不含结束日）')).toBeVisible();
  const block = report.blocks.find((b: { type: string }) => b.type === 'table');
  expect(block.rows).toHaveLength(52);
  const table = page.locator('.report-block').filter({ has: page.getByRole('heading', { name: block.title, exact: true }) });
  for (const column of block.columns) await expect(table.getByRole('columnheader', { name: column.label, exact: true })).toBeVisible();
  await table.locator('.ant-pagination-item').last().click();
  await expect(table.locator('tbody tr.ant-table-row').last()).toContainText(block.rows.at(-1)[2].display);
  await table.locator('.ant-pagination-item').first().click();
  await table.locator('tbody tr.ant-table-row').first().locator('button.evidence-value').nth(2).click();
  const drawer = page.getByRole('dialog', { name: '这个结果从哪里来', exact: true });
  await expect(drawer).toContainText(block.rows[0][2].display);
  await page.keyboard.press('Escape'); await expect(drawer).toBeHidden();
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: join(screenshots, `user-fix-${viewport.width}.png`), animations: 'disabled' });
  }
  expect(errors).toEqual([]);
});

for (const name of ['monthly', 'complex', 'followup', 'narrow']) {
  test(`stage7 actual ${name} report and evidence on desktop and mobile`, async ({ page }) => {
    const record = JSON.parse(readFileSync(join(directory, `${name}.json`), 'utf8'));
    expect(record.semantic_review.accepted).toBe(true);
    const report = record.report;
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    const screenshots = join(directory, 'screenshots'); mkdirSync(screenshots, { recursive: true });
    await page.goto(`/reports/${report.report_id}/revisions/${report.revision_id}`);
    await expect(page.getByRole('heading', { name: report.title, exact: true })).toBeVisible();
    await expect(page.getByText(`固定主范围：${report.date_range.start} 至 ${report.date_range.end_exclusive}（不含结束日）`)).toBeVisible();
    const block = report.blocks.find((b: { type: string }) => b.type === 'table');
    const table = page.locator('.report-block').filter({ has: page.getByRole('heading', { name: block.title, exact: true }) });
    await expect(table.locator('tbody tr.ant-table-row').first()).toContainText(block.rows[0][0].display);
    if (block.display_scope) await expect(table.getByText(/^分组维度：/)).toBeVisible();
    else for (const column of block.columns.filter((c: { name: string }) => ['month', 'day', 'carrier_code'].includes(c.name))) {
      await expect(table.getByRole('columnheader', { name: column.label, exact: true })).toBeVisible();
    }
    const position = block.columns.findIndex((c: { name: string }) => c.name === 'record_count');
    if (block.rows.length > 20) {
      await table.locator('.ant-pagination-item').last().click();
      await expect(table.locator('tbody tr.ant-table-row').last()).toContainText(block.rows.at(-1)[position].display);
      await table.locator('.ant-pagination-item').first().click();
    }
    await table.locator('tbody tr.ant-table-row').first().locator('button.evidence-value').nth(position).click();
    const drawer = page.getByRole('dialog', { name: '这个结果从哪里来', exact: true });
    await expect(drawer).toContainText(block.rows[0][position].display);
    await page.keyboard.press('Escape'); await expect(drawer).toBeHidden();
    if (name === 'followup') {
      const old = report.blocks.find((b: { type: string; artifact_id?: string }) => b.type === 'table' && b.artifact_id && record.artifacts[b.artifact_id].artifact.run_id !== record.run.id);
      const oldTable = page.locator('.report-block').filter({ has: page.getByRole('heading', { name: old.title, exact: true }) });
      await oldTable.locator('tbody tr.ant-table-row').first().locator('button.evidence-value').first().click();
      await expect(drawer).toContainText('引用旧轮次的证据');
      await expect(drawer).toContainText(record.artifacts[old.artifact_id].artifact.run_id);
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: join(screenshots, 'followup-old-evidence-mobile.png'), animations: 'disabled' });
      await page.keyboard.press('Escape'); await expect(drawer).toBeHidden();
    }
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: join(screenshots, `${name}-${viewport.width}.png`), animations: 'disabled' });
    }
    expect(errors).toEqual([]);
  });
}

test('real structured scope revision keeps original report and shows validated dimensions', async ({ page }) => {
  const record = JSON.parse(readFileSync(join(directory, 'structured-scope-revision.json'), 'utf8'));
  expect(record.original_hash_unchanged).toBe(true);
  expect(record.rejected_code).toBe('REPORT_SCOPE_POPULATION');
  const report = record.corrected_revision;
  await page.goto(`/reports/${report.report_id}/revisions/${report.revision_id}`);
  await expect(page.getByText(/^分组维度：/)).toBeVisible();
  await expect(page.getByText(/^人群（/)).toContainText('OH');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByText(/^分组维度：/).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: join(directory, 'screenshots', 'structured-scope-mobile.png'), animations: 'disabled' });
});
