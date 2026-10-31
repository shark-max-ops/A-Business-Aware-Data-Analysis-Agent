import { defineConfig } from '@playwright/test';

// 本地开发与验收配置。
export default defineConfig({
  testDir: './tests', testIgnore: ['knowledge.spec.ts', 'analysis.spec.ts'], use: { baseURL: 'http://127.0.0.1:5173', viewport: { width: 1440, height: 960 } },
  webServer: { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: !process.env.CI },
});
