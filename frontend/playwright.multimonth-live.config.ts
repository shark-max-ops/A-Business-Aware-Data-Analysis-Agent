import { defineConfig } from '@playwright/test';

// 仅连接用户明确启动的6.E隔离真实服务，不创建测试模型或触发模型请求。
export default defineConfig({
  testDir: './tests', testMatch: 'multimonth-live.spec.ts', workers: 1, timeout: 60000,
  use: { baseURL: 'http://127.0.0.1:5176', viewport: { width: 1440, height: 1000 } },
});
