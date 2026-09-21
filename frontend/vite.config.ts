import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  build: {
    // 本机的文件删除被"安全删除"shim 劫持（删除进回收站，经 genie-trash 可执行文件），
    // 大量小文件删除会超时导致构建失败（实测 error during build: [safe-delete] …ETIMEDOUT）。
    // 因此关闭 vite 自带的 emptyOutDir，构建前由外部用 PowerShell 预清理 dist。
    emptyOutDir: false,
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: true,
    proxy: {
      // 开发模式下把 /api 代理到统一后端（单一服务 :8080）。
      // 调某个模块时可指向隔离实例：set VITE_API_TARGET=http://localhost:8090
      // 这样改模块时不用重启线上 8080，其它工具照常使用。
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
