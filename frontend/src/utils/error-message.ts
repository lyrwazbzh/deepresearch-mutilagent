/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

/**
 * 从 catch 到的任意值里取后端返回的 `detail` 文案（本仓 axios 错误约定）。
 * 取不到时返回 undefined，由调用方给出业务兜底文案。
 *
 * 收敛历史写法 `error?.response?.data?.detail` —— 该写法要求 `error` 为 `any`；
 * `strict` 下 catch 变量是 `unknown`，故把这一次断言集中到此处。
 */
export function errorDetail(error: unknown): string | undefined {
  const err = error as { response?: { data?: { detail?: string } } }
  return err?.response?.data?.detail
}
