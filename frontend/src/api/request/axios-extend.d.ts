/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import 'axios'

declare module 'axios' {
  export interface AxiosRequestConfig {
    /**
     * 是否显示 loading 遮罩
     * plugins/loading.ts
     */
    loading?: boolean

    /**
     * 请求异常时是否显示 toast 提示
     * plugins/error-toast.ts
     */
    errorToast?: boolean

    /**
     * 取消重复请求
     * plugins/repeat.ts
     */
    cancelRepeat?: boolean
    repeatKey?: string
  }
}
