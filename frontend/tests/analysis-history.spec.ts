import { test, expect, type Page } from '@playwright/test';

test('report keeps fixed main range separate from declared population and subrange', async ({ page }) => {
  const range = { start: '2026-01-01', end_exclusive: '2026-03-01' };
  const report = { report_id: 'scope-test', revision_id: 'v1', version: 1, title: '合成范围报告',
    question: '合成问题', scope: '范围说明', date_range: range, created_at: '2026-01-01T00:00:00Z',
    data_snapshot: { id: 'synthetic', period: null, manifest: { source_kind: 'synthetic' } },
    limitations: ['仅合成展示验收'], draft: { limitations: [] }, blocks: [{
      id: 'groups', type: 'table', title: '指定航司', artifact_id: 'qa_test',
      columns: [{ name: 'carrier', label: '航司' }, { name: 'day', label: '日期' }],
      rows: [[{ raw: 'OH', display: 'OH', value_ref: { artifact_id: 'qa_test', row: 0, column: 'carrier' } },
        { raw: '2026-01-01', display: '2026-01-01', value_ref: { artifact_id: 'qa_test', row: 0, column: 'day' } }]],
      display_scope: { dimensions: ['carrier', 'day'], population: { carrier: ['OH'] },
        date_column: 'day', date_range: { start: '2026-01-01', end_exclusive: '2026-01-02' } },
    }] };
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname;
    const health = { llm: 'configured_unverified', analysis: 'ready', status: 'ok', stage: 4,
      database: 'ready', fts5: 'available', worker: 'ready', embedding: 'not_configured' };
    return route.fulfill({ json: path.endsWith('/health') ? health : path.endsWith('/v1') ? report : { items: [] } });
  });
  await page.goto('/reports/scope-test/revisions/v1');
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await expect(page.getByText('固定主范围：2026-01-01 至 2026-03-01（不含结束日）')).toBeVisible();
    await expect(page.getByText('日期子范围：2026-01-01 至 2026-01-02（不含结束日）')).toBeVisible();
    await expect(page.getByText('分组维度：航司、日期')).toBeVisible();
    await expect(page.getByText('人群（航司）：OH')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `../.cache/screenshots/stage7c-scope-${viewport.width}.png`, animations: 'disabled' });
  }
});

test('unverified ranking prose stays outside checked conclusions and collapsed by default', async ({ page }) => {
  const report = { report_id: 'claim-policy', revision_id: 'v1', version: 1, title: '合成核对报告',
    question: '合成排名问题', scope: '模型自由说明', verification_policy: 'checked_relations_v1',
    has_unverified_explanations: true, created_at: '2026-01-01T00:00:00Z',
    data_snapshot: { id: 'synthetic', manifest: { source_kind: 'synthetic' } }, limitations: [],
    draft: { limitations: [] }, blocks: [
      { id: 'checked', type: 'text', verification: 'checked_relation', proof: { comparison_rows: 52 },
        segments: [{ text: '服务端核对第三名为 AA，最低组为 B6。' }] },
      { id: 'unsafe', type: 'text', verification: 'unverified',
        segments: [{ text: '合成错误解释：YX第三高，AS最低。' }] },
    ] };
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith('/health') ? { status: 'ok', llm: 'configured_unverified', analysis: 'ready' }
      : path.endsWith('/v1') ? report : { items: [] } });
  });
  await page.goto(`${process.env.STAGE7_BASE_URL ?? ''}/reports/claim-policy/revisions/v1`);
  await expect(page.getByText('服务端核对第三名为 AA，最低组为 B6。', { exact: true })).toBeVisible();
  const pending = page.locator('.report-unverified');
  await expect(pending.locator('summary')).toContainText('不属于已核对结论');
  await expect(page.getByText('合成错误解释：YX第三高，AS最低。', { exact: true })).toBeHidden();
  await pending.locator('summary').click();
  await expect(page.getByText('合成错误解释：YX第三高，AS最低。', { exact: true })).toBeVisible();
  await expect(pending).toContainText('可能存在排名、分母或因果错误');
});

// 全部为浏览器合成响应；不创建真实调查或调用模型。
const timestamp = '2026-01-01T00:00:00Z';
function investigation(id: string) {
  return { id, conversation_id: 'history-session', goal: `目标 ${id}`, scope: `范围 ${id}`, snapshot_id: 'snap', knowledge_snapshot_id: 'knowledge', status: 'ready', active_run_id: null, findings: [], parent_id: null, version: 1, allocated: { runs: 1, max_turns: 1, max_tool_calls: 1, timeout_seconds: 1 }, limits: { runs: 40, max_turns: 400, max_tool_calls: 400, timeout_seconds: 4000 } };
}
function run(index: number, id: string | null = 'first') {
  return { id: `history-${index}`, conversation_id: 'history-session', investigation_id: id, question: `问题 ${index}`, created_at: new Date(Date.parse(timestamp) + index * 1000).toISOString(), status: 'completed', snapshot_id: 'snap', knowledge_snapshot_id: 'knowledge', result: { kind: 'clarification', question: `需要确认 ${index}`, missing_requirements: ['合成条件'] }, context_run_ids: [], metric_revision_ids: [], budget: {}, cancel_requested: false };
}
async function setup(page: Page, history: ReturnType<typeof run>[]) {
  const first = investigation('first'); const second = investigation('second');
  await page.addInitScript(() => localStorage.setItem('analysis-conversation', 'history-session'));
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    let json: unknown;
    if (path === '/health') json = { llm: 'configured_unverified', analysis: 'ready', status: 'ok', stage: 4, database: 'ready', fts5: 'available', worker: 'ready', embedding: 'not_configured' };
    else if (path === '/conversations') json = { items: [{ id: 'history-session', title: '历史合成会话', created_at: timestamp }] };
    else if (path === '/conversations/history-session') json = { items: history };
    else if (path === '/investigations') json = { items: [second, first] };
    else if (path === '/sources') json = { items: [{ id: 'source', source_kind: 'synthetic', current_snapshot_id: 'snap' }] };
    else if (path === '/sources/source/snapshots') json = { items: [{ id: 'snap', period: '2026-01', row_count: 3, manifest: { source_kind: 'synthetic' } }] };
    else if (path.endsWith('/event-log')) json = { items: [], next_cursor: null };
    else return route.fulfill({ status: 404, json: { error: { message: 'unexpected test request', code: path } } });
    await route.fulfill({ json });
  });
  await page.goto('/analysis');
  await expect(page.getByLabel('分析问题', { exact: true })).toBeEnabled();
}
async function select(page: Page, label: string) {
  await page.getByRole('combobox', { name: '切换调查', exact: true }).press('ArrowDown');
  await page.locator('.ant-select-item-option-content').getByText(label, { exact: true }).click();
}

test('multimonth snapshot quality and explicit default activation stay separate from preview', async ({ page }) => {
  await setup(page, []);
  await select(page, '目标 second');
  await page.route('**/api/v1/sources/source/snapshots', route => route.fulfill({ json: { items: [
    { id: 'multi', period: null, periods: ['2026-01', '2026-02'], coverage_start: '2026-01-01', coverage_end_exclusive: '2026-03-01', row_count: 6, created_at: timestamp, file_hash: 'collection', manifest: { source_kind: 'synthetic', sources: [{ period: '2026-01', rows: 3, date_min: '2026-01-01', date_max: '2026-01-03' }] }, quality_report: { negative_duration_counts: { scheduled_elapsed: 9 }, null_counts: { scheduled_elapsed: 1 } } },
    { id: 'snap', period: '2026-01', row_count: 3, created_at: timestamp, file_hash: 'original', manifest: { source_kind: 'synthetic' }, quality_report: {} },
  ] } }));
  await page.route('**/api/v1/sources/source/schema?*', route => route.fulfill({ json: { snapshot_id: 'multi', tables: [] } }));
  let activations = 0;
  await page.route('**/api/v1/sources/source/activate', async route => {
    activations++;
    expect(route.request().postDataJSON()).toEqual({ snapshot_id: 'multi' });
    if (activations === 1) await route.fulfill({ status: 409, json: { error: { code: 'TEST_FAILURE', message: '合成切换失败' } } });
    else await route.fulfill({ json: { id: 'source', name: '合成来源', source_kind: 'synthetic', current_snapshot_id: 'multi' } });
  });
  await page.getByRole('button', { name: '工作区数据源 ↗' }).click();
  const drawer = page.getByRole('dialog', { name: '数据源与固定快照' });
  await expect(drawer).toContainText('2026-01、2026-02');
  await expect(drawer.getByRole('button', { name: '设为默认快照', exact: true })).toBeEnabled();
  await expect(drawer).toContainText('原件集合 SHA-256');
  await expect(drawer.getByRole('region', { name: '快照覆盖与质量摘要' })).toContainText('计划时长：9');
  expect(activations).toBe(0);
  await drawer.getByRole('button', { name: '设为默认快照', exact: true }).click();
  await expect(drawer).toContainText('合成切换失败');
  await expect(drawer.getByRole('button', { name: /设为默认快照$/ })).toBeEnabled();
  expect(activations).toBe(1);
  await drawer.getByRole('button', { name: /设为默认快照$/ }).click();
  await expect(drawer.getByRole('button', { name: /已是默认快照$/ })).toBeDisabled();
  await drawer.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.workspace-toolbar-title')).toContainText('目标 second');
  await expect(page.locator('.fixed-date-range')).toContainText('2026-01-01 至 2026-02-01');
  expect(activations).toBe(2);
});

test('new investigation refuses a missing month and outside dates without changing the current scope', async ({ page }) => {
  await setup(page, [run(1)]);
  await page.route('**/api/v1/sources/source/snapshots', route => route.fulfill({ json: { items: [
    { id: 'snap', period: '2026-01', row_count: 3, manifest: { source_kind: 'synthetic' } },
    { id: 'gap', period: null, periods: ['2026-01', '2026-03'], coverage_start: '2026-01-01', coverage_end_exclusive: '2026-04-01', row_count: 6, manifest: { source_kind: 'synthetic' }, quality_report: {} },
  ] } }));
  await page.reload();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '新调查', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '新建调查', exact: true });
  await dialog.getByLabel('调查目标', { exact: true }).fill('缺月边界检查');
  await dialog.getByLabel('调查范围', { exact: true }).fill('明确合成测试');
  await dialog.getByRole('combobox', { name: '新调查数据快照', exact: true }).press('ArrowDown');
  await page.locator('.ant-select-item-option-content').filter({ hasText: '2026-01、2026-03' }).click();
  await expect(dialog).toContainText('所选范围缺少 2026-02 数据');
  await expect(dialog.getByRole('button', { name: '固定范围并创建', exact: true })).toBeDisabled();
  await dialog.getByLabel('结束日期（不含）', { exact: true }).fill('2026-02-01');
  await expect(dialog.getByRole('button', { name: '固定范围并创建', exact: true })).toBeEnabled();
  await dialog.getByLabel('开始日期（含）', { exact: true }).fill('2025-12-31');
  await expect(dialog).toContainText('日期范围超出所选快照覆盖');
  await expect(dialog.getByRole('button', { name: '固定范围并创建', exact: true })).toBeDisabled();
  await dialog.getByRole('button', { name: /取\s*消/ }).click();
  await expect(page.locator('.fixed-date-range')).toContainText('2026-01-01 至 2026-02-01');
});

test('frozen investigation and turn show the exact partial date range', async ({ page }) => {
  const date_range = { start: '2026-01-02', end_exclusive: '2026-01-03' };
  await setup(page, [run(1)]);
  await page.route('**/api/v1/conversations/history-session', route => route.fulfill({ json: {
    items: [{ ...run(1), date_range }],
  } }));
  await page.route('**/api/v1/investigations?*', route => route.fulfill({ json: {
    items: [{ ...investigation('first'), date_range }],
  } }));
  await page.reload();
  await page.getByRole('button', { name: '调查信息', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '调查信息', exact: true })).toContainText('2026-01-02 至 2026-01-03（不含结束日）');
  await page.keyboard.press('Escape');
  await page.locator('.run-result').getByText('本轮信息', { exact: true }).click();
  await expect(page.locator('.run-result')).toContainText('2026-01-02 至 2026-01-03（不含结束日）');
});

test('continuous history, investigation isolation, drafts, drawers and mobile layout', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  const logs: string[] = []; page.on('request', request => { if (request.url().includes('/event-log')) logs.push(request.url()); });
  await setup(page, [run(1), run(2), run(0, 'second')]);
  await expect(page.locator('.run-result')).toHaveCount(2);
  await expect(page.locator('.analysis-question h3')).toHaveText(['问题 1', '问题 2']);
  expect(logs).toHaveLength(0);
  await expect(page.locator('.analysis-thread')).not.toContainText('版本与累计额度');
  await page.getByRole('button', { name: '调查信息', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '调查信息', exact: true })).toContainText('已分配 1/40');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: '调查信息', exact: true })).toBeHidden();
  await page.getByLabel('分析问题', { exact: true }).fill('第一调查草稿');
  await select(page, '目标 second');
  await expect(page.locator('.run-result')).toHaveCount(1);
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('');
  await page.getByLabel('分析问题', { exact: true }).fill('第二调查草稿');
  await select(page, '目标 first');
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('第一调查草稿');
  await page.locator('.run-result').first().getByRole('button', { name: '执行详情' }).click();
  await expect(page.getByRole('dialog', { name: '执行详情' })).toContainText('本次运行尚无工具记录');
  expect(logs).toHaveLength(1);
  await page.keyboard.press('Escape');
  await page.reload();
  await expect(page.locator('.run-result')).toHaveCount(2);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: /调查发现 ·/ }).click();
  const drawer = page.getByRole('dialog', { name: '调查发现', exact: true });
  await expect(drawer).toContainText('核查后的发现');
  await expect.poll(() => drawer.evaluate(e => e.getBoundingClientRect().width <= innerWidth + 1)).toBe(true);
  await page.keyboard.press('Escape');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeInViewport();
  await page.screenshot({ path: '../.cache/screenshots/continuous-history-mobile.png' });
  expect(errors).toEqual([]);
});

test('older turns are batched and locating a turn does not change the draft target', async ({ page }) => {
  await setup(page, Array.from({ length: 25 }, (_, i) => run(i)));
  await expect(page.locator('.run-result')).toHaveCount(20);
  await page.locator('.analysis-thread').evaluate(e => { e.scrollTop = 0; });
  const before = await page.locator('#turn-history-5').evaluate(e => e.getBoundingClientRect().top);
  await page.getByRole('button', { name: '显示更早轮次' }).click();
  await expect(page.locator('.run-result')).toHaveCount(25);
  expect(Math.abs(await page.locator('#turn-history-5').evaluate(e => e.getBoundingClientRect().top) - before)).toBeLessThan(5);
  await expect(page.locator('.analysis-question h3').first()).toHaveText('问题 0');
  await page.getByLabel('分析问题', { exact: true }).fill('保留此草稿');
  await page.getByRole('combobox', { name: '定位轮次', exact: true }).press('ArrowDown');
  await page.locator('.ant-select-item-option-content').getByText('问题 0 · 已完成', { exact: true }).click();
  await expect(page.locator('#turn-history-0')).toBeInViewport();
  await page.getByRole('combobox', { name: '定位轮次', exact: true }).press('ArrowDown');
  await page.locator('.ant-select-item-option-content').getByText('问题 1 · 已完成', { exact: true }).click();
  await expect(page.locator('#turn-history-1')).toBeInViewport();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('保留此草稿');
});

test('leaving a turn stops reading further event pages and ignores its late response', async ({ page }) => {
  await setup(page, [run(1), run(0, 'second')]);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let started = false; let nextPage = 0;
  await page.route('**/runs/history-1/event-log?*', async route => {
    if (new URL(route.request().url()).searchParams.get('after') !== '0') { nextPage++; return route.fulfill({ json: { items: [], next_cursor: null } }); }
    started = true; await gate;
    await route.fulfill({ json: { items: [{ run_id: 'history-1', seq: 1, type: 'run.progress', payload: {}, created_at: timestamp }], next_cursor: '1' } });
  });
  await page.locator('.execution-toggle').click();
  await expect.poll(() => started).toBe(true);
  await select(page, '目标 second');
  release();
  await expect(page.locator('.analysis-question h3')).toHaveText('问题 0');
  // 给已释放的请求机会完成；第二页请求代表离开后仍继续读取。
  await page.waitForTimeout(300);
  expect(nextPage).toBe(0);
});

test('a newly created investigation retains the question when run submission fails', async ({ page }) => {
  await setup(page, []);
  await page.route('**/api/v1/investigations*', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 201, json: investigation('created') }) : route.fulfill({ json: { items: [] } }));
  await page.route('**/api/v1/runs', route => route.fulfill({ status: 503, json: { error: { message: '合成提交失败', code: 'TEST_SUBMIT' } } }));
  await page.reload();
  await page.getByLabel('分析问题', { exact: true }).fill('失败时仍须保留的问题');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await expect(page.getByText(/合成提交失败/)).toBeVisible();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('失败时仍须保留的问题');
});

test('a missing saved conversation releases navigation instead of permanently disabling the page', async ({ page }) => {
  await setup(page, [run(1)]);
  await page.route('**/api/v1/conversations/history-session', route => route.fulfill({ status: 404, json: { error: { message: '会话已不存在', code: 'CONVERSATION_NOT_FOUND' } } }));
  await page.reload();
  await expect(page.getByText(/会话已不存在/)).toBeVisible();
  await expect(page.getByRole('button', { name: '新会话', exact: true })).toBeEnabled();
  expect(await page.evaluate(() => localStorage.getItem('analysis-conversation'))).toBeNull();
});

test('first question survives creating a conversation when investigation creation fails', async ({ page }) => {
  await setup(page, []);
  await page.route('**/api/v1/conversations', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 201, json: { id: 'new-session', title: '新会话', created_at: timestamp } })
    : route.fulfill({ json: { items: [] } }));
  await page.route('**/api/v1/conversations/new-session', route => route.fulfill({ json: { items: [] } }));
  await page.route('**/api/v1/investigations*', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { error: { message: '合成调查创建失败', code: 'TEST_CREATE' } } })
    : route.fulfill({ json: { items: [] } }));
  await page.evaluate(() => localStorage.removeItem('analysis-conversation'));
  await page.reload();
  await page.getByLabel('分析问题', { exact: true }).fill('首次问题不能丢失');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await expect(page.getByText(/合成调查创建失败/)).toBeVisible();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('首次问题不能丢失');
});

test('partial event history retries from the last page and does not invent complete statistics', async ({ page }) => {
  await setup(page, [run(1)]);
  let failed = false;
  const cursors: string[] = [];
  await page.route('**/runs/history-1/event-log?*', async route => {
    const after = new URL(route.request().url()).searchParams.get('after')!; cursors.push(after);
    if (after === '0') return route.fulfill({ json: { items: Array.from({ length: 100 }, (_, i) => ({ run_id: 'history-1', seq: i + 1, type: 'run.progress', payload: {}, created_at: timestamp })), next_cursor: '100' } });
    if (!failed) { failed = true; return route.fulfill({ status: 503, json: { error: { code: 'TEST_OFFLINE', message: '合成断线' } } }); }
    return route.fulfill({ json: { items: [{ run_id: 'history-1', seq: 101, type: 'run.completed', payload: {}, created_at: timestamp }], next_cursor: null } });
  });
  await page.locator('.execution-toggle').click();
  await expect(page.getByText('执行记录尚未完整读取', { exact: true })).toBeVisible();
  await expect(page.getByText('本次运行没有工具执行记录。', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '重试记录' }).click();
  await expect(page.getByText('本次运行没有工具执行记录。', { exact: true })).toBeVisible();
  expect(cursors).toEqual(['0', '100', '100']);
});

test('legacy turns are read only and remain outside investigations', async ({ page }) => {
  await setup(page, [run(1), run(0, null)]);
  await select(page, '历史独立运行');
  await expect(page.locator('.analysis-question h3')).toHaveText('问题 0');
  await expect(page.getByLabel('分析问题', { exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '发送问题' })).toBeDisabled();
  await expect(page.getByText('历史独立运行仅供查看，继续分析请选择或新建调查')).toBeVisible();
});

test('failed turn remains visible after continuation and request keeps existing context semantics', async ({ page }) => {
  const failed = Object.assign(run(1), { status: 'failed', result: null, error_code: 'FINAL_CANDIDATE_INVALID' });
  await setup(page, [failed]);
  await expect(page.getByText('本轮未保存调查发现。', { exact: false })).toBeVisible();
  let body: Record<string, unknown> = {};
  await page.route('**/api/v1/runs', async route => {
    body = route.request().postDataJSON();
    await route.fulfill({ status: 202, json: { ...run(2), question: body.question } });
  });
  await page.getByLabel('分析问题', { exact: true }).fill('失败后继续核查');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await expect(page.locator('.run-result')).toHaveCount(2);
  await expect(page.locator('.run-result').first()).toContainText('本次分析未完成');
  expect(body.investigation_id).toBe('first');
  expect(body).not.toHaveProperty('context_run_ids');
});

test('query pagination stays with its artifact and an isolated read error is retryable', async ({ page }) => {
  const first = Object.assign(run(1), { result: { kind: 'query_result', artifact_ids: ['artifact-one'] } });
  const second = Object.assign(run(2), { result: { kind: 'query_result', artifact_ids: ['artifact-two'] } });
  await setup(page, [first, second]);
  let fail = true;
  await page.route('**/api/v1/runs/*/artifacts/*', async route => {
    const url = new URL(route.request().url()); const id = url.pathname.split('/').at(-1)!;
    const offset = Number(url.searchParams.get('offset'));
    if (id === 'artifact-two' && fail) { return route.fulfill({ status: 503, json: { error: { message: '合成读取失败', code: 'TEST_READ' } } }); }
    return route.fulfill({ json: { artifact: { id, returned_rows: 40, truncated: true, columns: [{ name: '测试值' }] }, rows: [[`${id}-${offset}`]], offset, next_offset: offset ? null : 20 } });
  });
  await page.reload();
  const turns = page.locator('.run-result');
  await expect(turns.last()).toContainText('查询结果读取失败');
  fail = false;
  await turns.last().getByRole('button', { name: '重试结果' }).click();
  await expect(turns.last()).toContainText('artifact-two-0');
  await turns.first().scrollIntoViewIfNeeded();
  await expect(turns.first()).toContainText('artifact-one-0');
  await turns.first().getByRole('button', { name: '下一页' }).click();
  await expect(turns.first()).toContainText('artifact-one-20');
  await expect(turns.last()).toContainText('artifact-two-0');
  await expect(turns.first()).toContainText('结果已截断，不能作为完整统计');
});

test('each turn keeps its findings, historical suggestions and fixed information alongside the summary', async ({ page }) => {
  await setup(page, [run(1), run(2)]);
  const first = investigation('first');
  const finding = (id: string, runId: string, text: string, questions: string[], supersedes: string | null = null) => ({ id, run_id: runId, text, questions, supersedes, kind: 'fact', check: '合成核查', source_run_status: 'completed', artifact_ids: [], evidence_ids: [], value_refs: {}, created_at: timestamp });
  Object.assign(first, { findings: [finding('old', 'history-1', '第一轮独有发现', ['历史续问']), finding('new', 'history-2', '第二轮修正发现', ['最新续问'], 'old')] });
  await page.route('**/api/v1/investigations*', route => route.fulfill({ json: { items: [first] } }));
  await page.reload();
  const turns = page.locator('.run-result');
  await expect(turns.first()).toContainText('第一轮独有发现');
  await expect(turns.first()).toContainText('已有后续修正');
  await expect(turns.first()).toContainText('历史建议');
  await expect(turns.first()).not.toContainText('第二轮修正发现');
  await expect(turns.last()).toContainText('第二轮修正发现');
  await expect(turns.last()).not.toContainText('第一轮独有发现');
  await turns.first().getByRole('button', { name: '历史续问', exact: true }).click();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('历史续问');
  await turns.last().getByRole('button', { name: '最新续问', exact: true }).click();
  const confirm = page.getByRole('dialog', { name: '替换当前草稿？' });
  await confirm.getByRole('button', { name: '保留草稿' }).click();
  await expect(page.getByLabel('分析问题', { exact: true })).toHaveValue('历史续问');
  await turns.first().getByText('本轮信息', { exact: true }).click();
  await expect(turns.first()).toContainText('已记录模型用量：未记录');
  await page.getByRole('button', { name: /调查发现 ·/ }).click();
  const summary = page.getByRole('region', { name: '当前调查' });
  await expect(summary.locator('.investigation-finding:not(.historical)')).toHaveCount(1);
  await expect(summary.getByRole('button', { name: '最新续问', exact: true })).toBeVisible();
  await expect(summary.getByRole('button', { name: '历史续问', exact: true })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await turns.first().getByRole('button', { name: '定位后续修正轮次' }).click();
  await expect(turns.last()).toBeInViewport();
});

test('event reading failure is visible and retryable inside the execution drawer', async ({ page }) => {
  await setup(page, [run(1)]);
  let fail = true;
  await page.route('**/runs/history-1/event-log?*', route => fail
    ? route.fulfill({ status: 502, contentType: 'text/html', body: '<html>test proxy error</html>' })
    : route.fulfill({ json: { items: [], next_cursor: null } }));
  await page.getByRole('button', { name: '执行详情', exact: true }).click();
  const drawer = page.getByRole('dialog', { name: '执行详情', exact: true });
  await expect(drawer).toContainText('响应无法解析（HTTP 502）');
  await expect(drawer).not.toContainText('本次运行尚无工具记录');
  fail = false;
  await drawer.getByRole('button', { name: '重试记录', exact: true }).click();
  await expect(drawer).toContainText('本次运行尚无工具记录');
  await expect(drawer).not.toContainText('响应无法解析');
});

test('turn data is locked to its old snapshot and source versions stay distinct', async ({ page }) => {
  await setup(page, [run(1)]);
  const first = investigation('first');
  Object.assign(first, { findings: ['a', 'b'].map(id => ({ id, run_id: 'history-1', kind: 'fact', text: `发现 ${id}`, check: '合成', questions: [], source_run_status: 'completed', supersedes: null, artifact_ids: [], evidence_ids: [`revision-${id}`] })) });
  await page.route('**/api/v1/investigations*', route => route.fulfill({ json: { items: [first] } }));
  await page.route('**/api/v1/investigations/first/findings/*/evidence', route => {
    const id = route.request().url().includes('/a/') ? 'a' : 'b';
    return route.fulfill({ json: { block: { evidence_ids: [`revision-${id}`], segments: [] }, artifacts: {}, sources: { [`revision-${id}`]: { title: '相同标题资料', content: `固定版本 ${id}`, source_kind: 'official_reference', locator: { revision: id } } } } });
  });
  await page.route('**/api/v1/sources', route => route.fulfill({ json: { items: [{ id: 'source', name: '合成来源', source_kind: 'synthetic', current_snapshot_id: 'latest' }] } }));
  await page.route('**/api/v1/sources/source/snapshots', route => route.fulfill({ json: { items: [
    { id: 'latest', source_id: 'source', period: '2026-02', row_count: 8, manifest: {}, quality_report: {}, created_at: timestamp },
    { id: 'snap', source_id: 'source', period: '2026-01', row_count: 3, manifest: {}, quality_report: {}, created_at: timestamp, file_hash: 'synthetic-hash' },
  ] } }));
  await page.route('**/api/v1/sources/source/schema?*', route => route.fulfill({ json: { tables: [] } }));
  await page.reload();
  const turn = page.locator('.run-result');
  await turn.getByText('本轮数据与依据', { exact: true }).click();
  await expect(turn.locator('.turn-source-index details')).toHaveCount(2);
  await turn.locator('.turn-source-index details').first().locator('summary').click();
  await expect(turn).toContainText('固定版本 a');
  await turn.locator('.turn-source-index details').last().locator('summary').click();
  await expect(turn).toContainText('固定版本 b');
  await turn.getByRole('button', { name: '查看本轮固定数据' }).click();
  const drawer = page.getByRole('dialog', { name: '本轮固定数据快照', exact: true });
  await expect(drawer).toContainText('2026-01');
  await expect(drawer.getByRole('combobox')).toHaveCount(0);
  await expect(drawer.getByRole('button', { name: '设为默认快照' })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => drawer.evaluate(e => e.getBoundingClientRect().width <= innerWidth + 1)).toBe(true);
  await page.screenshot({ animations: 'disabled', path: '../.cache/screenshots/turn-fixed-snapshot-mobile.png' });
  await page.keyboard.press('Escape');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('a degraded model service does not hide available data or claim there are no snapshots', async ({ page }) => {
  await setup(page, [run(1)]);
  await page.route('**/api/v1/health', route => route.fulfill({ status: 503, json: { status: 'degraded', stage: 4, database: 'ready', fts5: 'available', worker: 'not_ready', analysis: 'not_ready', llm: 'not_configured', embedding: 'not_configured' } }));
  await page.reload();
  await expect(page.getByRole('combobox', { name: '本次数据快照', exact: true }).locator('..').locator('..')).toContainText('3 条');
  await expect(page.getByText('暂无可分析的数据，请先导入数据快照', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeDisabled();
  await expect(page.locator('.run-result')).toHaveCount(1);
});

test('the source index excludes uncited materials from the frozen report pool', async ({ page }) => {
  await setup(page, [Object.assign(run(1), { result: { kind: 'report', report_id: 'report', revision_id: 'rev' } })]);
  const source = (id: string) => ({ id, revision_id: id, title: id === 'bound' ? '实际引用资料' : '未引用的搜索命中', source_kind: 'official_reference', content: id, locator: {} });
  await page.route('**/api/v1/reports/report/revisions/rev', route => route.fulfill({ json: { report_id: 'report', revision_id: 'rev', run_id: 'history-1', title: '合成报告', limitations: [], sources: [source('bound'), source('unused')], blocks: [{ id: 'text', type: 'text', segments: [{ text: '报告文字' }], evidence_ids: ['bound'] }] } }));
  await page.route('**/api/v1/reports/report/revisions/rev/evidence?*', route => route.fulfill({ json: { block_id: 'text', artifact: null, sources: [source('bound')], metrics: [] } }));
  await page.reload();
  const turn = page.locator('.run-result');
  await turn.getByText('本轮数据与依据', { exact: true }).click();
  await expect(turn.locator('.turn-source-index')).toContainText('实际引用资料');
  await expect(turn.locator('.turn-source-index')).not.toContainText('未引用的搜索命中');
});

test('partial reports stay visibly partial in history and the full report', async ({ page }) => {
  await setup(page, [Object.assign(run(1), { result: { kind: 'report', report_id: 'partial-report', revision_id: 'rev', partial: true } })]);
  const report = { report_id: 'partial-report', revision_id: 'rev', run_id: 'history-1',
    title: '合成部分结果', partial: true, limitations: ['候选解释未通过校验'], sources: [],
    version: 1, scope: '合成范围', data_snapshot: { id: 'snap', manifest: {} }, created_at: timestamp,
    draft: { limitations: [] }, blocks: [{ id: 'text', type: 'text', segments: [{ text: '有效结果已保留' }], evidence_ids: [] }] };
  await page.route('**/api/v1/reports/partial-report/revisions/rev', route => route.fulfill({ json: report }));
  await page.route('**/api/v1/reports/partial-report/revisions', route => route.fulfill({ json: { items: [] } }));
  await page.reload();
  const turn = page.locator('.run-result');
  await expect(turn.locator('.execution-title')).toHaveText('部分完成，已保留核查结果');
  await expect(turn.getByText('部分完成：已保留核查结果', { exact: true })).toBeVisible();
  await expect(turn.getByText('分析已完成', { exact: true })).toHaveCount(0);
  await turn.getByRole('link', { name: '打开完整报告 ↗' }).click();
  await expect(page.getByText('部分完成：已保留核查结果，解释仍需复核', { exact: true })).toBeVisible();
  await expect(page.getByText('候选解释未通过校验', { exact: true })).toBeVisible();
});

test('investigation refresh failure is not displayed as zero saved findings or no references', async ({ page }) => {
  await setup(page, [Object.assign(run(1), { status: 'failed', result: null, error_code: 'MODEL_REQUEST_FAILED' })]);
  await page.goto('about:blank');
  await page.route('**/api/v1/investigations?*', async route => {
    // StrictMode会重复发起初始化请求；只对已显示轮次后的刷新注入失败。
    if (await page.locator('.run-result').count() === 0) return route.fulfill({ json: { items: [investigation('first')] } });
    return route.fulfill({ status: 503, json: { error: { code: 'TEST_READ', message: '调查刷新失败' } } });
  });
  await page.goto('/analysis');
  const turn = page.locator('.run-result');
  await expect(turn).toContainText('暂不能确认本轮已保存发现数量');
  await expect(turn).not.toContainText('本轮未保存调查发现。');
  await turn.getByText('本轮数据与依据', { exact: true }).click();
  await expect(turn).toContainText('暂不能确认本轮发现引用是否完整');
  await expect(turn).not.toContainText('本轮没有附加的发现或报告资料引用');
});
