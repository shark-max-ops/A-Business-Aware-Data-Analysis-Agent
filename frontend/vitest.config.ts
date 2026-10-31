import { defineConfig } from 'vitest/config';

// 单元测试与浏览器测试分别运行。
export default defineConfig({ test: { include: ['src/**/*.test.{ts,tsx}'] } });
