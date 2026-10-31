/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { AxiosResponse } from 'axios'

/**
 * 取响应体 —— **`api/` 模块层**的取体出口（T70）。
 *
 * 本 client **不解包**（`unwrap` 契约已于 T70 删除，见 `./request.ts`）：
 * 调用方拿到的始终是 `AxiosResponse`，响应体在 `response.data`。
 * `api/` 下需要响应体的模块一律经此取体，不再各自写
 * `const res = await …; return res.data`（`api/news.ts` 曾把同一句抄 8 遍 ——
 * 约定只该有一个归属地）。**新增**取体代码也一律走这里。
 *
 * ⚠️ 口径边界（§4 补审 / T72 修正）：本文件此前宣称「**全仓唯一**的出口」，
 * **该说法不成立**，已收严。实测（`grep -rn readBody frontend/src`）本函数当前
 * 使用者只有 `api/news.ts`；`api/` 层确实已收拢，但 `store/` 与 `pages/` 仍有
 * 大量调用方直接读 `response.data` —— 例如 `store/session.ts`（4 处）、
 * `store/knowledge.ts`、`pages/chat/index.tsx`、`pages/database/index.tsx`、
 * `pages/memory/index.tsx`。把全仓收拢到一处是**独立范围**（几十处调用点、
 * 纯行为等价迁移），不在 T70 之内；在完成之前不得声称已统一。
 *
 * 将来若要改回「client 解包」，只需改这里一处 —— 但**前提是所有调用方都经此取体**，
 * 上述未迁移的调用方不在此保证之内。
 */
export async function readBody<T>(response: Promise<AxiosResponse<T>>): Promise<T> {
  return (await response).data
}
