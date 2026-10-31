/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { createContext } from 'react'
import { createBrowserRouter } from 'react-router-dom'

type RouterInstance = ReturnType<typeof createBrowserRouter>

// Provider（router/index.tsx）恒在组件树中包裹本 Context，null 仅作惰性初始值
export const RouterContext = createContext<RouterInstance>(
  null as unknown as RouterInstance,
)
