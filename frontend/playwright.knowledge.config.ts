import { defineConfig } from '@playwright/test';

// 隔离元数据库和服务端口，浏览器写入不会污染用户的知识与审核状态。
export default defineConfig({
  testDir: './tests', testMatch: 'knowledge.spec.ts', workers: 1, timeout: 90000,
  use: { baseURL: 'http://127.0.0.1:5174', viewport: { width: 1440, height: 1000 } },
  webServer: [
    { command: '..\\.venv\\Scripts\\python.exe ..\\scripts\\browser_knowledge_server.py', url: 'http://127.0.0.1:8001/api/v1/health', reuseExistingServer: true, timeout: 60000 },
    { command: 'npm run dev -- --port 5174', url: 'http://127.0.0.1:5174', env: { API_PROXY_TARGET: 'http://127.0.0.1:8001' }, reuseExistingServer: true },
  ],
});
