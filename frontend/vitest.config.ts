import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': '/src',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    maxWorkers: 1,
    setupFiles: ['./src/test/setup.ts'],
    // 默认 5000ms 对这套 jsdom 用例太紧：OutlineApprovalPanel / deep-research-integration 里
    // 的重交互用例单条实测就要 2.6–2.9s（离 5s 只剩 1.7 倍余量），CPU 争用下会越过 5s 被判超时
    // （T43 用 6 份并发跑复现出 3–4 例 `Test timed out in 5000ms`）。放宽到 20000ms。
    // 断言本身与墙钟无关（唯一涉及计时的用例走 fake timers），故这里只是给渲染成本留余量，
    // 不掩盖真实缺陷：真挂死的用例仍会在 20s 内失败。
    testTimeout: 20000,
  },
})
