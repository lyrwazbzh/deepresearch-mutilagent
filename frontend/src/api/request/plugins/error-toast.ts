/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { AxiosRequestConfig, AxiosResponse, CanceledError } from 'axios'
import { ResponseError } from '../error'
import { IRequestPlugin } from './plugin'

// 仅 429 有专属文案；其余状态码统一走下方兜底链（ResponseError.message → 后端 message/error → 通用文案）。
// 曾有一份覆盖 400–505 的映射表但全部被注释（T36：YAGNI，未按需补全，直接删除）
const NETWORK_ERROR_MAP: Record<string, string> = {
  429: 'Too Many Requests, please try again later',
}

export const errorToastPlugin: IRequestPlugin = {
  postinstall(instance) {
    instance.interceptors.response.use(
      (response) => response,
      (error) => {
        const response = error.response as AxiosResponse<{ message?: string; error?: string }> | undefined
        const config = (response?.config ?? error?.config) as AxiosRequestConfig

        if (config && !config.errorToast) return Promise.reject(error)

        // CanceledError 主要来源于 repeat.ts 取消重复请求
        // 该错误不应展示给用户
        if (error instanceof CanceledError) return Promise.reject(error)

        const status = response?.status ?? ''
        const message =
          error instanceof ResponseError
            ? error.message
            : NETWORK_ERROR_MAP[status as keyof typeof NETWORK_ERROR_MAP] ||
              response?.data?.message ||
              response?.data?.error ||
              error.message ||
              'Request Error'

        window.$app.message.error(message)

        return Promise.reject(error)
      },
    )
  },
}
