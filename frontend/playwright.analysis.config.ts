import { defineConfig } from '@playwright/test';

// 使用独立数据库、明确的测试模型和真实 SQL 执行器，不触碰实际指标。
export default defineConfig({
  testDir: './tests', testMatch: 'analysis.spec.ts', workers: 1, timeout: 60000,
  use: { baseURL: 'http://127.0.0.1:5175', viewport: { width: 1440, height: 1000 } },
  // Windows 下可显式管理隔离服务的启动与退出，测试仍访问相同真实前后端。
  webServer: process.env.PLAYWRIGHT_EXTERNAL_SERVER === '1' ? undefined : [
    { command: '..\\.venv\\Scripts\\python.exe ..\\scripts\\browser_analysis_server.py', url: 'http://127.0.0.1:8002/api/v1/health', reuseExistingServer: true, timeout: 60000 },
    { command: 'npm run dev -- --port 5175', url: 'http://127.0.0.1:5175', env: { API_PROXY_TARGET: 'http://127.0.0.1:8002' }, reuseExistingServer: true },
  ],
});
