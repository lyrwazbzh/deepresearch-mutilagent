/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { createRequest } from './request'

// T70：此处曾写 `unwrap: true`，但没有任何插件读取它 —— 声明了一个不存在的契约，
// 于是每个调用方都得自己知道该 client 的私有约定（`api/news.ts` 曾手写 8 次取体）。
// 删除而非实现：声明的信封形状 `{ code, msg, data }` 在本仓库后端不存在
// （后端一律直接返回响应体），实现它等于为无人遵守的契约写代码。
// 取响应体一律走 `./read-body`，那是全仓唯一的出口。
export const request = createRequest({
  baseURL: import.meta.env.VITE_API_BASE,
  loading: true,
  errorToast: true,
  cancelRepeat: true,
})
