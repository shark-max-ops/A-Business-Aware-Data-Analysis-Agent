import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 本地开发与验收配置。
export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8000' } },
});
