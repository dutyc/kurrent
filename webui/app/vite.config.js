import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 管理面不监听 TCP：开发代理指向 nginx 443（TOFU 自签证书，secure:false），
      // 保留 /api/cp 前缀由 nginx 剥离后经 unix socket 转发。
      '/api/cp': {
        target: process.env.VITE_CP_PROXY || 'https://127.0.0.1',
        changeOrigin: true,
        secure: false
      },
      // 引导链状态页对 /tftp/ 做 HEAD 探测：确认脚本确实能经 HTTPS 取回，而不只是落在盘上。
      '/tftp': {
        target: process.env.VITE_CP_PROXY || 'https://127.0.0.1',
        changeOrigin: true,
        secure: false
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets'
  }
})
