import { expect, Page, Route, test } from '@playwright/test'

const outlineEvent = {
  type: 'outline_pending_approval',
  session_id: 'session-1',
  outline_revision: 'revision-1',
  sections: [1, 2, 3, 4, 5].map((index) => ({
    id: `section-${index}`,
    title: `Chapter ${index}`,
    description: `Description ${index}`,
    section_type: 'mixed',
    requires_data: index <= 2,
    requires_chart: index <= 2,
  })),
  research_questions: [1, 2, 3].map((index) => ({
    id: `question-${index}`,
    text: `Question ${index}`,
  })),
}

function sse(events: unknown[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('')
}

async function mockApplication(
  page: Page,
  options: { staleApproval?: boolean } = {},
) {
  let outlineRequests = 0
  let resumeRequests = 0
  let approved = false
  let approvalBody: Record<string, unknown> | undefined
  const persistedAssistantMessages: Record<string, unknown>[] = []

  await page.route('**/*', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
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
      return route.fulfill({
        json: { success: true, data: [], total: 0, stats: {} },
      })
    }
    if (path.endsWith('/sessions') && method === 'POST') {
      return route.fulfill({
        json: {
          id: 'session-1',
          title: 'Battery industry outlook',
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
        ? [{ role: 'user', content: 'Battery industry outlook' }]
        : []
      return route.fulfill({
        json: {
          id: 'session-1',
          title: 'Battery industry outlook',
          session_type: 'deepsearch',
          created_at: '2026-07-19T00:00:00Z',
          updated_at: '2026-07-19T00:00:00Z',
          message_count: messages.length,
          messages,
        },
      })
    }
    if (path.endsWith('/sessions/session-1/messages') && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      if (body.role === 'assistant') persistedAssistantMessages.push(body)
      return route.fulfill({ json: { id: 'message-1' } })
    }
    if (path.endsWith('/research/checkpoint/session-1/full')) {
      if (!approved) return route.fulfill({ json: { success: false } })
      return route.fulfill({
        json: {
          success: true,
          checkpoint: {
            id: 'checkpoint-1',
            session_id: 'session-1',
            query: 'Battery industry outlook',
            phase: 'researching',
            status: 'running',
            final_report: null,
            state_json: { references: [], charts: [] },
            ui_state_json: {
              research_steps: [],
              search_results: [],
              charts: [],
              knowledge_graph: null,
              streaming_report: '',
            },
          },
        },
      })
    }
    if (path.endsWith('/research/stream') && method === 'POST') {
      outlineRequests += 1
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse([outlineEvent]),
      })
    }
    if (
      path.endsWith('/research/outline/session-1/approve') &&
      method === 'POST'
    ) {
      approvalBody = request.postDataJSON()
      if (options.staleApproval) {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Outline revision is stale' }),
        })
      }
      approved = true
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse([
          { type: 'phase', phase: 'planning', content: 'Generating queries' },
        ]),
      })
    }
    if (path.endsWith('/research/resume/session-1') && method === 'POST') {
      resumeRequests += 1
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse([
          { type: 'phase', phase: 'researching', content: 'Resumed' },
          { type: 'research_complete', final_report: 'Resumed final report' },
        ]),
      })
    }
    if (path.endsWith('/research/cancel/session-1')) {
      return route.fulfill({ json: { success: true } })
    }

    return route.fallback()
  })

  return {
    get outlineRequests() {
      return outlineRequests
    },
    get resumeRequests() {
      return resumeRequests
    },
    get approvalBody() {
      return approvalBody
    },
    get persistedAssistantMessages() {
      return persistedAssistantMessages
    },
  }
}

async function loginAndStartResearch(page: Page) {
  await page.goto('/login')
  await page.getByPlaceholder('用户名或邮箱').fill('researcher')
  await page.getByPlaceholder('密码').fill('password')
  await page.getByRole('button', { name: /登\s*录/ }).click()
  await expect(page).toHaveURL(/\/chat$/)

  await page.getByRole('button', { name: '搜索模式' }).click()
  await page.getByRole('checkbox', { name: '深度搜索（网络）' }).check()
  const sender = page.getByPlaceholder('按 Enter 发送，Shift + Enter 换行')
  await sender.fill('Battery industry outlook')
  await sender.press('Enter')
  await expect(page).toHaveURL(/\/chat\/session-1$/)
  await expect(page.getByText('审核研究大纲')).toBeVisible()
}

test('approve, disconnect, and resume without regenerating the outline', async ({
  page,
}) => {
  const state = await mockApplication(page)
  await loginAndStartResearch(page)

  const workspace = page.getByRole('region', {
    name: '研究大纲审核工作区',
  })
  const workspaceBox = await workspace.boundingBox()
  const chatBox = await page.locator('.com-page-layout__main').boundingBox()
  expect(workspaceBox?.width).toBeGreaterThan(chatBox?.width ?? 0)

  await page.getByLabel('第 1 章标题').fill('Edited market overview')
  await page
    .getByRole('textbox', { name: '研究问题 1', exact: true })
    .fill('Edited core question')
  await page.getByRole('button', { name: '确认大纲并开始研究' }).click()

  await expect.poll(() => state.approvalBody).toMatchObject({
    outline_revision: 'revision-1',
    sections: expect.arrayContaining([
      expect.objectContaining({ title: 'Edited market overview' }),
    ]),
    research_questions: expect.arrayContaining([
      expect.objectContaining({ text: 'Edited core question' }),
    ]),
  })
  await expect(page.getByText('审核研究大纲')).toBeHidden()

  await page.reload()
  await expect(page.getByText('Resumed final report')).toBeVisible()
  expect(state.outlineRequests).toBe(1)
  expect(state.resumeRequests).toBe(1)
  expect(state.persistedAssistantMessages).toEqual([
    expect.objectContaining({
      role: 'assistant',
      content: 'Resumed final report',
    }),
  ])
})

test('a stale revision keeps the edited draft', async ({ page }) => {
  await mockApplication(page, { staleApproval: true })
  await loginAndStartResearch(page)

  await page.getByLabel('第 1 章标题').fill('Draft that must survive')
  await page.getByRole('button', { name: '确认大纲并开始研究' }).click()

  await expect(page.getByText('Outline revision is stale')).toBeVisible()
  await expect(page.getByLabel('第 1 章标题')).toHaveValue(
    'Draft that must survive',
  )
  await expect
    .poll(() =>
      page.evaluate(() =>
        localStorage.getItem(
          'deep-research-outline-draft:session-1:revision-1',
        ),
      ),
    )
    .toContain('Draft that must survive')
})

test('the approval workspace remains usable on a narrow screen', async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 900 })
  await mockApplication(page)
  await loginAndStartResearch(page)

  const workspace = page.getByRole('region', {
    name: '研究大纲审核工作区',
  })
  await workspace.scrollIntoViewIfNeeded()
  await expect(workspace).toBeVisible()
  await expect(
    page.getByRole('button', { name: '确认大纲并开始研究' }),
  ).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
})
