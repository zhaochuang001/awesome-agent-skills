import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 构建产物输出到 ../web，由 fleet_service.py 作为静态目录直接提供。
// 开发模式走代理访问本地 fleet 服务（127.0.0.1:8790）。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../web',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8790',
    },
  },
});
