/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { lazy, Suspense } from 'react'
import PageLoading from '@/components/page-loading'
import { AuthGuard } from '@/components/auth-guard'
import { BaseLayout } from '@/layout/base'
import {
  Navigate,
  Outlet,
  RouteObject,
  createBrowserRouter,
} from 'react-router-dom'

// T34：路由级页面全部懒加载（组件内部不拆分）
const Index = lazy(() => import('@/pages/index'))
const NewChat = lazy(() => import('@/pages/chat/newchat'))
const Chat = lazy(() => import('@/pages/chat'))
const KnowledgePage = lazy(() => import('@/pages/knowledge'))
const MemoryPage = lazy(() => import('@/pages/memory'))
const DatabasePage = lazy(() => import('@/pages/database'))
const NewsPage = lazy(() => import('@/pages/news'))
const BiddingPage = lazy(() => import('@/pages/bidding'))
const NotFound = lazy(() => import('@/pages/404'))
const LoginPage = lazy(() => import('@/pages/auth/login'))

export type IRouteObject = {
  children?: IRouteObject[]
  name?: string
  auth?: boolean
  pure?: boolean
  meta?: Record<string, unknown>
} & Omit<RouteObject, 'children'>

export const routes: IRouteObject[] = [
  {
    path: '/',
    Component: Index,
  },
  {
    path: '/chat',
    children: [
      {
        path: '',
        Component: NewChat,
      },
      {
        path: ':id',
        Component: Chat,
      },
    ],
  },
  {
    path: '/knowledge',
    Component: KnowledgePage,
  },
  {
    path: '/memory',
    Component: MemoryPage,
  },
  {
    path: '/database',
    Component: DatabasePage,
  },
  {
    path: '/news',
    Component: NewsPage,
  },
  {
    path: '/bidding',
    Component: BiddingPage,
  },
  {
    path: '/404',
    Component: NotFound,
    pure: true,
  },
]

export const router = createBrowserRouter(
  [
    {
      path: '/login',
      element: (
        <Suspense fallback={<PageLoading />}>
          <LoginPage />
        </Suspense>
      ),
    },
    {
      path: '/',
      element: (
        <AuthGuard>
          <BaseLayout>
            {/* 单一 Suspense 边界覆盖全部懒加载子路由 */}
            <Suspense fallback={<PageLoading />}>
              <Outlet />
            </Suspense>
          </BaseLayout>
        </AuthGuard>
      ),
      children: routes,
    },
    {
      path: '*',
      element: <Navigate to="/404" />,
    },
  ] as RouteObject[],
  {
    basename: import.meta.env.BASE_URL,
  },
)
