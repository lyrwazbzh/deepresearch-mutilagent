/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import axios, { AxiosRequestConfig } from 'axios'
import { authPlugin } from './plugins/auth'
import { errorToastPlugin } from './plugins/error-toast'
import { loadingPlugin } from './plugins/loading'
import { installPlugins } from './plugins/plugin'
import { repeatPlugin } from './plugins/repeat'
import { servicePlugin } from './plugins/service'

/**
 * 创建一个请求实例。
 *
 * **契约（T70 起显式声明）**：返回原生 axios 实例，**不对响应体做任何解包** ——
 * 调用方拿到的始终是 `AxiosResponse`，响应体在 `response.data`
 * （取体一律走 `./read-body` 的 `readBody()`）。
 *
 * 此前这里声明过一个 `unwrap` 选项（「把 `response.data.data` 提升到
 * `response.data`」），但没有任何插件读取它 —— 声明了一个不存在的契约。
 * T70 选择**删除而非实现**：该选项面向的信封形状 `{ code, msg, data }`
 * 在本仓库后端不存在（后端一律直接返回响应体），且全仓没有一处 `.data.data`
 * 消费点；实现它等于为无人遵守的契约写代码。另需注意，若按声明语义「最小实现」
 * （`response.data = data.data`），会顺手改写「碰巧带 `data` 字段」的响应体 ——
 * 例如 `/news/list` 的 `{ success, data, total, stats }` 会丢掉 `total` / `stats`。
 */
export function createRequest(configs: AxiosRequestConfig = {}) {
  const instance = axios.create(configs)

  installPlugins(instance, [
    authPlugin,
    servicePlugin,
    loadingPlugin,
    repeatPlugin,
    errorToastPlugin,
  ])

  return instance
}
