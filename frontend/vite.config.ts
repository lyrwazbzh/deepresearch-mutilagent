/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'
import { viteMockServe } from 'vite-plugin-mock'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd()) as {
    VITE_API_BASE: string
    VITE_API_PROXY: string
  }

  return {
    server: {
      port: 5183,
      host: '0.0.0.0',
      proxy: {
        [env.VITE_API_BASE]: env.VITE_API_PROXY,
      },
    },
    resolve: {
      alias: [
        {
          find: /^@\//,
          replacement: '/src/',
        },
      ],
    },

    build: {
      // 生产不产出 sourcemap（避免产物显著增大）
      sourcemap: false,
      rollupOptions: {
        output: {
          // T34：仅手工拆出最重的 3 个 vendor chunk，不做更细的分包调优
          manualChunks(id) {
            if (!id.includes('node_modules')) return undefined
            if (id.includes('echarts')) return 'echarts'
            if (id.includes('antd') || id.includes('@ant-design')) return 'antd'
            if (id.includes('react')) return 'react'
            return undefined
          },
        },
      },
    },

    plugins: [
      react(),
      viteMockServe({
        enable: false,
      }),
    ],
  }
})
