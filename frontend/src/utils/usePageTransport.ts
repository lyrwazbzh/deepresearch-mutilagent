/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { useMount } from 'ahooks'
import { useState } from 'react'

const tempMap = new Map<PageTransportKey<unknown>, unknown>()

// 幽灵类型：仅用于把「同一个 symbol 键」与它承载的数据类型绑定，运行时只是一个 symbol。
// 用 type + symbol 交叉，替代原 `extends Symbol {}`（后者触发 no-empty-object-type / no-wrapper-object-types）。
export type PageTransportKey<T> = symbol & { readonly __pageTransportKey?: T }

/**
 * 用于页面间数据传输
 * 需要注意的是，仅在组件初始化时有效
 */
export function usePageTransport<T>(key: PageTransportKey<T>) {
  const [data, setData] = useState<T | undefined>(() => tempMap.get(key) as T | undefined)

  useMount(() => {
    const tempData = tempMap.get(key) as T | undefined
    setData(tempData)
    tempMap.delete(key)
  })

  return {
    data,
    setData,
  }
}

export function setPageTransport<T>(key: PageTransportKey<T>, data: T) {
  tempMap.set(key, data)
}
