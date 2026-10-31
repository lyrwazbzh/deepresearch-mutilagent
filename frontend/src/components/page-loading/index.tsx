/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import ComSpinner from '@/components/spin/spinner'

/**
 * 路由懒加载统一加载态（T34）：复用既有 ComSpinner，提供中文文案。
 */
export default function PageLoading() {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 12,
        padding: 48,
      }}
    >
      <ComSpinner />
      <span>页面加载中…</span>
    </div>
  )
}
