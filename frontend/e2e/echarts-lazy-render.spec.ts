/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 *
 * T35 的风险条款此前**没有可执行的验证**：
 *
 *   > 若 `echarts-for-react` 与动态 `import('echarts')` 的实例不共享，可能出现
 *   > 「图表不渲染」或「主题丢失」**必须**在浏览器中实际打开一个含图表的页面验证，
 *   > 不能只靠构建通过。
 *
 * 当时的结论是「本机无浏览器自动化环境，仅以构建产物 + 单元测试佐证」——
 * 即构建绿 ≠ 真出图。本文件把该条款变成可判定的断言。
 *
 * 判据（全部机器可判定，不依赖人工看图）：
 *
 * 1. **懒加载确实「按需」**：进入研究详情页后、点击图表 tab 之前，不得发生任何
 *    echarts 相关模块请求；点击之后必须发生。
 * 2. **图表真的画出来了**：图表容器的 `<canvas>` 非零尺寸，且其像素中存在
 *    **不透明**像素（CSS 背景不计入 canvas 像素，故「有不透明像素」= echarts 真的 draw 了），
 *    并且**颜色数 > 1**（排除纯色块 —— 坐标轴 / 柱体 / 文字会带来多种颜色）。
 * 3. **无未捕获异常**，且无 echarts 相关的 `console.error`。
 *
 * 全程走 mock，不起后端、不调 LLM。图表数据经 SSE 事件注入，路径与真实运行时一致
 * （`research_step` → `knowledge_graph` → `charts`）。
 */
import { expect, Page, Route, test } from '@playwright/test'

const NODES = [
  { id: 'n1', name: '新型储能', type: 'core', importance: 9 },
  { id: 'n2', name: '液流电池', type: 'tech', importance: 6 },
  { id: 'n3', name: '锂离子电池', type: 'tech', importance: 7 },
  { id: 'n4', name: '宁德时代', type: 'company', importance: 8 },
  { id: 'n5', name: '容量电价政策', type: 'policy', importance: 5 },
  { id: 'n6', name: '储能变流器', type: 'product', importance: 4 },
]

const EDGES = [
  { source: 'n1', target: 'n2', relation: '技术路线' },
  { source: 'n1', target: 'n3', relation: '技术路线' },
  { source: 'n3', target: 'n4', relation: '主要供应商' },
  { source: 'n1', target: 'n5', relation: '受政策驱动' },
  { source: 'n1', target: 'n6', relation: '关键部件' },
]

const CHART_TITLE = '2020–2025 中国新型储能累计装机（GW）'

const CHART = {
  id: 'chart-1',
  title: CHART_TITLE,
  subtitle: 'e2e 桩数据',
  type: 'bar',
  echarts_option: {
    color: ['#5470c6'],
    grid: { left: '3%', right: '4%', bottom: '8%', containLabel: true },
    xAxis: {
      type: 'category',
      data: ['2020', '2021', '2022', '2023', '2024', '2025'],
    },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', name: '装机', data: [3.3, 5.7, 13.1, 34.5, 73.8, 120] }],
  },
}

const outlineEvent = {
  type: 'outline_pending_approval',
  session_id: 'session-1',
  outline_revision: 'revision-1',
  // 注意：`planIsValid` 要求章节数与研究问题数均 ≥ 3 且逐项非空，
  // 否则「确认大纲并开始研究」按钮保持 disabled。
  sections: [1, 2, 3].map((index) => ({
    id: `section-${index}`,
    title: `Chapter ${index}`,
    description: `Description ${index}`,
    section_type: 'mixed',
    requires_data: index === 1,
    requires_chart: index === 1,
  })),
  research_questions: [1, 2, 3].map((index) => ({
    id: `question-${index}`,
    text: `Question ${index}`,
  })),
}

/** 审批后回灌的 SSE：与真实 V2 流水线同序 —— step → graph → charts */
const approvalEvents = [
  { type: 'phase', phase: 'researching', content: '检索阶段' },
  {
    type: 'research_step',
    content: {
      step_type: 'researching',
      status: 'running',
      title: '信息检索',
      stats: { results_count: 2, sources_count: 2 },
    },
  },
  {
    type: 'search_results',
    content: {
      results: [
        { id: 'r1', title: '储能装机统计', source: '国家能源局', url: 'https://example.com/a', snippet: 'A' },
        { id: 'r2', title: '储能产业白皮书', source: 'CNESA', url: 'https://example.com/b', snippet: 'B' },
      ],
    },
  },
  {
    type: 'research_step',
    content: {
      step_type: 'analyzing',
      status: 'running',
      title: '数据分析',
      stats: { charts_count: 1, entities_count: NODES.length },
    },
  },
  {
    type: 'knowledge_graph',
    content: {
      graph: { nodes: NODES, edges: EDGES },
      stats: { entitiesCount: NODES.length, relationsCount: EDGES.length },
    },
  },
  { type: 'charts', content: { charts: [CHART] } },
]

function sse(events: unknown[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('')
}

/** 记录所有请求 URL，用于断言 echarts 模块「按需」加载 */
function trackEchartsRequests(page: Page): string[] {
  const urls: string[] = []
  page.on('request', (request) => {
    const url = request.url()
    if (/echarts/i.test(url)) urls.push(url)
  })
  return urls
}

/** 真实后端源；任何漏过桩的请求都会打到它 */
const BACKEND_ORIGIN = /localhost:8001|127\.0\.0\.1:8001/

async function mockApplication(page: Page) {
  let approved = false
  // 走到 route handler 末尾仍没匹配上、且目标是真实后端的请求 = 桩漏了端点
  const unmockedRequests: string[] = []

  await page.route('**/*', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()

    if (path.endsWith('/auth/login') && method === 'POST') {
      return route.fulfill({
        json: {
          access_token: 'test-token',
          token_type: 'bearer',
          user: {
            id: 'user-1',
            username: 'researcher',
            email: 'researcher@example.com',
            is_active: true,
            created_at: '2026-07-19T00:00:00Z',
          },
        },
      })
    }
    if (path.endsWith('/news/list') || path.endsWith('/news/bidding/list')) {
      return route.fulfill({ json: { success: true, data: [], total: 0, stats: {} } })
    }
    if (path.endsWith('/sessions') && method === 'POST') {
      return route.fulfill({
        json: {
          id: 'session-1',
          title: 'Energy storage outlook',
          session_type: 'deepsearch',
          created_at: '2026-07-19T00:00:00Z',
          updated_at: '2026-07-19T00:00:00Z',
          message_count: 0,
        },
      })
    }
    if (path.endsWith('/sessions') && method === 'GET') {
      return route.fulfill({ json: [] })
    }
    if (path.endsWith('/sessions/session-1') && method === 'GET') {
      const messages = approved
        ? [{ role: 'user', content: 'Energy storage outlook' }]
        : []
      return route.fulfill({
        json: {
          id: 'session-1',
          title: 'Energy storage outlook',
          session_type: 'deepsearch',
          created_at: '2026-07-19T00:00:00Z',
          updated_at: '2026-07-19T00:00:00Z',
          message_count: messages.length,
          messages,
        },
      })
    }
    if (path.endsWith('/sessions/session-1/messages') && method === 'POST') {
      return route.fulfill({ json: { id: 'message-1' } })
    }
    if (path.endsWith('/research/checkpoint/session-1/full')) {
      // 不提供可恢复检查点，避免触发 resume 分支
      return route.fulfill({ json: { success: false } })
    }
    if (path.endsWith('/research/sessions/session-1/timeline')) {
      // 链路诊断面板的数据源；不桩会落到真实后端并产生 ERR_CONNECTION_REFUSED
      return route.fulfill({
        json: { runs: [], events: [], next_cursor: null },
      })
    }
    if (path.endsWith('/knowledge-bases') && method === 'GET') {
      // 打开「搜索模式」下拉时按需拉取（T52 引入）；空列表即可
      return route.fulfill({ json: [] })
    }
    if (path.endsWith('/research/stream') && method === 'POST') {
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse([outlineEvent]),
      })
    }
    if (path.endsWith('/research/outline/session-1/approve') && method === 'POST') {
      approved = true
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse(approvalEvents),
      })
    }
    if (path.endsWith('/research/cancel/session-1')) {
      return route.fulfill({ json: { success: true } })
    }

    if (BACKEND_ORIGIN.test(request.url())) {
      unmockedRequests.push(`${method} ${path}`)
    }
    return route.fallback()
  })

  return {
    get unmockedRequests() {
      return unmockedRequests
    },
  }
}

async function startResearchAndApprove(page: Page) {
  await page.goto('/login')
  await page.getByPlaceholder('用户名或邮箱').fill('researcher')
  await page.getByPlaceholder('密码').fill('password')
  await page.getByRole('button', { name: /登\s*录/ }).click()
  await expect(page).toHaveURL(/\/chat$/)

  await page.getByRole('button', { name: '搜索模式' }).click()
  await page.getByRole('checkbox', { name: '深度搜索（网络）' }).check()
  const sender = page.getByPlaceholder('按 Enter 发送，Shift + Enter 换行')
  await sender.fill('Energy storage outlook')
  await sender.press('Enter')
  await expect(page).toHaveURL(/\/chat\/session-1$/)

  await expect(page.getByText('审核研究大纲')).toBeVisible()
  await page.getByRole('button', { name: '确认大纲并开始研究' }).click()
  await expect(page.getByText('审核研究大纲')).toBeHidden()
}

/** 取页面上所有 canvas 的像素统计，判定「是否真的画了东西」 */
type CanvasPaint = {
  width: number
  height: number
  opaque: number
  distinctColors: number
}

async function readCanvasPaints(page: Page): Promise<CanvasPaint[]> {  return page.evaluate(() =>
    Array.from(document.querySelectorAll('canvas')).map((element) => {
      const canvas = element as HTMLCanvasElement
      const ctx = canvas.getContext('2d')
      if (!ctx || canvas.width === 0 || canvas.height === 0) {
        return { width: canvas.width, height: canvas.height, opaque: 0, distinctColors: 0 }
      }
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height)
      let opaque = 0
      const colors = new Set<number>()
      for (let i = 0; i < data.length; i += 4) {
        if (data[i + 3] > 0) {
          opaque += 1
          colors.add((data[i] << 16) | (data[i + 1] << 8) | data[i + 2])
        }
      }
      return {
        width: canvas.width,
        height: canvas.height,
        opaque,
        distinctColors: colors.size,
      }
    }),
  )
}

test('图表懒加载组件在真实浏览器中确实渲染出图（T35 风险条款）', async ({ page }) => {
  const mock = await mockApplication(page)
  const echartsRequests = trackEchartsRequests(page)
  const consoleErrors: string[] = []
  const pageErrors: string[] = []
  const badResponses: string[] = []
  const failedRequests: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('pageerror', (error) => pageErrors.push(`${error.name}: ${error.message}`))
  page.on('response', (response) => {
    if (response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`)
  })
  page.on('requestfailed', (request) => {
    failedRequests.push(`${request.url()} :: ${request.failure()?.errorText}`)
  })

  await startResearchAndApprove(page)

  // ---- 前置：数据确实经 SSE 注入到详情面板 ----
  await expect(page.getByRole('button', { name: /可视化图表/ })).toBeVisible()

  // ---- 判据 1：点图表 tab 之前，不得已经加载 echarts ----
  expect(
    echartsRequests,
    `进入详情页即加载了 echarts，懒加载失效：${echartsRequests.join(', ')}`,
  ).toEqual([])

  // ---- 判据 2：点「可视化图表」→ 图表容器出现 ----
  await page.getByRole('button', { name: /可视化图表/ }).click()
  await expect(page.getByText(CHART_TITLE)).toBeVisible()

  // echarts 模块此时才被请求
  await expect
    .poll(() => echartsRequests.length, {
      message: '点击图表 tab 后仍未请求 echarts 模块，懒加载组件没被挂载',
      timeout: 20_000,
    })
    .toBeGreaterThan(0)

  // ---- 判据 3：canvas 真的被绘制（不透明像素 + 多色）----
  await expect
    .poll(
      async () => {
        const paints = await readCanvasPaints(page)
        return paints.some(
          (paint) => paint.width > 0 && paint.height > 0 && paint.opaque > 0,
        )
      },
      { message: '图表 canvas 始终为空 —— echarts 没有真正绘制', timeout: 20_000 },
    )
    .toBe(true)

  const paints = await readCanvasPaints(page)
  const best = paints.reduce((acc, item) => (item.opaque > acc.opaque ? item : acc), paints[0])
  expect(best.width).toBeGreaterThan(0)
  expect(best.height).toBeGreaterThan(0)
  expect(best.opaque, 'canvas 中没有任何不透明像素').toBeGreaterThan(0)
  expect(
    best.distinctColors,
    'canvas 只有单一颜色，疑似纯色块而非真实图表',
  ).toBeGreaterThan(1)

  // ---- 判据 4：桩必须自足（否则「无错误」是假的），且无未捕获异常 / 无图表相关 console.error ----
  expect(
    mock.unmockedRequests,
    `桩漏了端点，请求放行到了真实后端：${mock.unmockedRequests.join(' | ')}`,
  ).toEqual([])
  expect(
    pageErrors,
    `出现未捕获异常：${pageErrors.join(' | ')}\n` +
      `HTTP>=400：${badResponses.join(' | ') || '（无）'}\n` +
      `请求失败：${failedRequests.join(' | ') || '（无）'}\n` +
      `console.error：${consoleErrors.join(' | ') || '（无）'}`,
  ).toEqual([])
  expect(
    consoleErrors.filter((text) => /echarts|chart/i.test(text)),
    `出现图表相关 console.error：${consoleErrors.join(' | ')}`,
  ).toEqual([])
})

test('知识图谱 tab 同样渲染出图（懒加载 echarts-for-react 的第二处挂载点）', async ({
  page,
}) => {
  const mock = await mockApplication(page)
  const echartsRequests = trackEchartsRequests(page)
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(`${error.name}: ${error.message}`))

  await startResearchAndApprove(page)

  expect(echartsRequests).toEqual([])

  await page.getByRole('button', { name: /知识图谱/ }).click()
  await expect(page.getByText(/个实体/)).toBeVisible()

  await expect
    .poll(() => echartsRequests.length, { timeout: 20_000 })
    .toBeGreaterThan(0)

  await expect
    .poll(
      async () => {
        const paints = await readCanvasPaints(page)
        return paints.some((paint) => paint.opaque > 0 && paint.distinctColors > 1)
      },
      { message: '知识图谱 canvas 未绘制', timeout: 20_000 },
    )
    .toBe(true)

  expect(
    mock.unmockedRequests,
    `桩漏了端点，请求放行到了真实后端：${mock.unmockedRequests.join(' | ')}`,
  ).toEqual([])
  expect(pageErrors, `出现未捕获异常：${pageErrors.join(' | ')}`).toEqual([])
})
