import { defineConfig } from '@playwright/test';

// 只读取显式启动的阶段7隔离服务与既有报告，不触发模型请求。
export default defineConfig({
  testDir: './tests', testMatch: 'stage7-live.spec.ts', workers: 1, timeout: 60000,
  use: { baseURL: process.env.STAGE7_BASE_URL ?? 'http://127.0.0.1:5177', viewport: { width: 1440, height: 1000 } },
});
