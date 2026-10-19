import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 构建产物直接落到 Python 包里（`src/lyra/web`），
 * 服务端只认这一个目录——不存在「前端仓库与运行时两份文件」的可能。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../src/lyra/web',
    emptyOutDir: true,
    // 内网部署，不需要为省几 KB 去拆 vendor 包；单块更大但请求更少，
    // 首次加载反而更快，且避免「循环依赖导致 chunk 顺序错乱」这类问题。
    chunkSizeWarningLimit: 2000,
    assetsInlineLimit: 0,
  },
  server: {
    port: 5273,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: false },
      '/favicon.svg': { target: 'http://127.0.0.1:8080' },
    },
  },
})
