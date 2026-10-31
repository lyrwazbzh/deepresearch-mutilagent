import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  deepsearch: vi.fn(),
  approveResearchOutline: vi.fn(),
  resumeResearch: vi.fn(),
  cancelResearch: vi.fn(),
  addMessage: vi.fn(),
  getSession: vi.fn(),
  getFullResearchCheckpoint: vi.fn(),
  getResearchTimeline: vi.fn(),
  getAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
  uploadAttachment: vi.fn(),
  chat: vi.fn(),
  chatWithAttachments: vi.fn(),
}))

vi.mock('@/api', () => ({ session: apiMocks }))
vi.mock('@/utils', () => ({
  usePageTransport: () => ({ data: { ctx: undefined }, dataReady: true }),
}))
const deviceMocks = vi.hoisted(() => ({
  // P-15：改为可变，便于逐用例切换搜索模式与知识库
  state: { searchModes: ['web'] as string[], kbName: '' },
}))

vi.mock('@/store/device', () => ({
  deviceState: deviceMocks.state,
  deviceActions: { setChatting: vi.fn() },
}))
vi.mock('@/store/session', () => ({
  sessionState: { currentSession: null },
}))
vi.mock('ahooks', () => ({ useUnmount: vi.fn() }))
vi.mock('@/components/page-layout', () => ({
  default: ({ children, sender, right }: {
    children: React.ReactNode
    sender: React.ReactNode
    right?: React.ReactNode
  }) => (
    <main>
      <section data-testid="chat-main">
        {children}
        {sender}
      </section>
      <aside data-testid="right-workspace">{right}</aside>
    </main>
  ),
}))
vi.mock('@/components/sender', () => ({
  default: ({ onSend }: { onSend: (value: string) => void }) => (
    <button onClick={() => onSend('Battery industry outlook')}>Start research</button>
  ),
}))
vi.mock('./component/chat-message', () => ({
  default: ({ list }: {
    list: Array<{
      content?: string
      reference?: Array<{ title: string; link: string }>
      reactSteps?: Array<{ content?: string }>
    }>
  }) => (
    <div data-testid="chat-messages">
      {list.map((item, index) => (
        <div key={index}>
          {item.content}
          {item.reactSteps?.map((step, stepIndex) => (
            <div key={stepIndex}>{step.content}</div>
          ))}
          {item.reference?.map((reference) => (
            <a key={reference.title} href={reference.link}>
              {reference.title}
            </a>
          ))}
        </div>
      ))}
    </div>
  ),
}))
vi.mock('./component/drawer', () => ({ default: () => null }))
vi.mock('./component/source', () => ({ default: () => null }))
vi.mock('./component/step-detail-panel', () => ({ default: () => null }))
vi.mock('./component/research-detail', () => ({ default: () => null }))

import ChatPage from './index'

function eventStream(events: unknown[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const event of events) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
      }
      controller.close()
    },
  })
}

function jsonStream(value: unknown): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(JSON.stringify(value)))
      controller.close()
    },
  })
}

function outlineEvent() {
  return {
    type: 'outline_pending_approval',
    session_id: 'session-1',
    outline_revision: 'revision-1',
    sections: [1, 2, 3].map((index) => ({
      id: `section-${index}`,
      title: `Chapter ${index}`,
      description: `Description ${index}`,
      section_type: 'mixed',
      requires_data: false,
      requires_chart: false,
    })),
    research_questions: [1, 2, 3].map((index) => ({
      id: `question-${index}`,
      text: `Question ${index}`,
    })),
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/chat/session-1']}>
      <Routes>
        <Route path="/chat/:id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function SessionSwitcher() {
  const navigate = useNavigate()
  return (
    <button onClick={() => navigate('/chat/session-2')}>
      Switch session
    </button>
  )
}

function renderPageWithSessionSwitcher() {
  return render(
    <MemoryRouter initialEntries={['/chat/session-1']}>
      <SessionSwitcher />
      <Routes>
        <Route path="/chat/:id" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('deep research outline approval integration', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    deviceMocks.state.searchModes = ['web']
    deviceMocks.state.kbName = ''
    apiMocks.getSession.mockResolvedValue({ data: { messages: [] } })
    apiMocks.getFullResearchCheckpoint.mockResolvedValue({
      data: { success: false },
    })
    apiMocks.getResearchTimeline.mockResolvedValue({
      data: { runs: [], events: [], next_cursor: null },
    })
    apiMocks.addMessage.mockResolvedValue({ data: {} })
    apiMocks.deepsearch.mockResolvedValue({
      data: eventStream([outlineEvent()]),
    })
  })

  it('reloads the durable research timeline when a session opens', async () => {
    renderPage()

    await waitFor(() =>
      expect(apiMocks.getResearchTimeline).toHaveBeenCalledWith('session-1'),
    )
  })

  it('keeps the live research process when delayed history contains only the user turn', async () => {
    const user = userEvent.setup()
    let resolveSession!: (value: unknown) => void
    apiMocks.getSession.mockReturnValue(
      new Promise((resolve) => {
        resolveSession = resolve
      }),
    )
    apiMocks.deepsearch.mockResolvedValue({
      data: eventStream([
        {
          type: 'research_step',
          content: {
            step_id: 'researching',
            step_type: 'researching',
            title: 'Information retrieval',
            status: 'running',
          },
        },
        { type: 'thought', content: 'Live DeepScout reasoning' },
      ]),
    })
    renderPage()

    await waitFor(() => expect(apiMocks.getSession).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: 'Start research' }))
    expect(await screen.findByText('Live DeepScout reasoning')).toBeInTheDocument()

    await act(async () => {
      resolveSession({
        data: {
          messages: [{ role: 'user', content: 'Battery industry outlook' }],
        },
      })
    })

    expect(screen.getByText('Live DeepScout reasoning')).toBeInTheDocument()
  })

  it('submits edited content once and consumes the approval stream', async () => {
    const user = userEvent.setup()
    apiMocks.approveResearchOutline.mockResolvedValue({
      data: eventStream([
        { type: 'phase', phase: 'planning', content: 'Generating queries' },
        { type: 'research_complete', final_report: 'Approved research report' },
      ]),
    })
    renderPage()

    await user.click(screen.getByRole('button', { name: 'Start research' }))
    const firstTitle = await screen.findByLabelText('第 1 章标题')
    expect(
      within(screen.getByTestId('right-workspace')).getByText(
        '审核研究大纲',
      ),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('chat-main')).queryByText(
        '审核研究大纲',
      ),
    ).not.toBeInTheDocument()
    await user.clear(firstTitle)
    await user.type(firstTitle, 'Edited market overview')
    const approveButton = screen.getByRole('button', {
      name: '确认大纲并开始研究',
    })
    await user.dblClick(approveButton)

    await waitFor(() =>
      expect(apiMocks.approveResearchOutline).toHaveBeenCalledTimes(1),
    )
    expect(apiMocks.approveResearchOutline).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({
        outline_revision: 'revision-1',
        sections: expect.arrayContaining([
          expect.objectContaining({ title: 'Edited market overview' }),
        ]),
      }),
      expect.objectContaining({ errorToast: false }),
    )
    expect(await screen.findByText('Approved research report')).toBeInTheDocument()
    expect(screen.queryByText('审核研究大纲')).not.toBeInTheDocument()
    expect(
      localStorage.getItem(
        'deep-research-outline-draft:session-1:revision-1',
      ),
    ).toBeNull()
  })

  it('keeps the edited panel and draft when approval returns 409', async () => {
    const user = userEvent.setup()
    apiMocks.approveResearchOutline.mockRejectedValue({
      response: { data: jsonStream({ detail: 'Outline revision is stale' }) },
    })
    renderPage()

    await user.click(screen.getByRole('button', { name: 'Start research' }))
    const firstTitle = await screen.findByLabelText('第 1 章标题')
    await user.clear(firstTitle)
    await user.type(firstTitle, 'Preserved edit')
    await user.click(
      screen.getByRole('button', { name: '确认大纲并开始研究' }),
    )

    expect(await screen.findByText('Outline revision is stale')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Preserved edit')).toBeInTheDocument()
    await waitFor(
      () => {
        const raw = localStorage.getItem(
          'deep-research-outline-draft:session-1:revision-1',
        )
        expect(raw).toContain('Preserved edit')
      },
      { timeout: 1500 },
    )
  })

  it('uses the shared stream path when resuming a checkpoint', async () => {
    apiMocks.getFullResearchCheckpoint.mockResolvedValue({
      data: {
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
    apiMocks.resumeResearch.mockResolvedValue({
      data: eventStream([
        { type: 'phase', phase: 'researching', content: 'Resumed' },
        { type: 'research_complete', final_report: 'Resumed research report' },
      ]),
    })

    renderPage()

    expect(await screen.findByText('Resumed research report')).toBeInTheDocument()
    expect(apiMocks.resumeResearch).toHaveBeenCalledWith('session-1')
    expect(apiMocks.deepsearch).not.toHaveBeenCalled()
  })

  it('restores and resumes when only the user message was persisted', async () => {
    let resolveCheckpoint!: (value: unknown) => void
    const checkpointResponse = new Promise((resolve) => {
      resolveCheckpoint = resolve
    })

    apiMocks.getSession.mockResolvedValue({
      data: {
        messages: [
          { role: 'user', content: 'NVIDIA growth outlook' },
        ],
      },
    })
    apiMocks.getFullResearchCheckpoint.mockReturnValue(checkpointResponse)
    apiMocks.resumeResearch.mockResolvedValue({
      data: eventStream([]),
    })

    renderPage()

    expect(await screen.findByText('NVIDIA growth outlook')).toBeInTheDocument()

    resolveCheckpoint({
      data: {
        success: true,
        checkpoint: {
          id: 'checkpoint-1',
          session_id: 'session-1',
          query: 'NVIDIA growth outlook',
          phase: 'reviewing',
          status: 'running',
          final_report: 'Durable report draft',
          state_json: { references: [], charts: [] },
          ui_state_json: {
            research_steps: [],
            search_results: [],
            charts: [],
            knowledge_graph: null,
            streaming_report: 'Durable report draft',
          },
        },
      },
    })

    expect(await screen.findByText('Durable report draft')).toBeInTheDocument()
    expect(apiMocks.resumeResearch).toHaveBeenCalledWith('session-1')
    expect(apiMocks.addMessage).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({
        role: 'assistant',
        content: 'Durable report draft',
      }),
    )
    expect(apiMocks.deepsearch).not.toHaveBeenCalled()
  })

  it('materializes a completed checkpoint when the assistant message is missing', async () => {
    let resolveCheckpoint!: (value: unknown) => void
    const checkpointResponse = new Promise((resolve) => {
      resolveCheckpoint = resolve
    })

    apiMocks.getSession.mockResolvedValue({
      data: {
        messages: [
          { role: 'user', content: 'Completed research question' },
        ],
      },
    })
    apiMocks.getFullResearchCheckpoint.mockReturnValue(checkpointResponse)

    renderPage()

    expect(await screen.findByText('Completed research question')).toBeInTheDocument()

    resolveCheckpoint({
      data: {
        success: true,
        checkpoint: {
          id: 'checkpoint-1',
          session_id: 'session-1',
          query: 'Completed research question',
          phase: 'completed',
          status: 'completed',
          final_report: 'Completed durable report',
          state_json: { references: [], charts: [] },
          ui_state_json: {
            research_steps: [],
            search_results: [],
            charts: [],
            knowledge_graph: null,
            streaming_report: 'Completed durable report',
          },
        },
      },
    })

    expect(await screen.findByText('Completed durable report')).toBeInTheDocument()
    await waitFor(() =>
      expect(apiMocks.addMessage).toHaveBeenCalledWith(
        'session-1',
        expect.objectContaining({
          role: 'assistant',
          content: 'Completed durable report',
        }),
      ),
    )
    expect(apiMocks.resumeResearch).not.toHaveBeenCalled()
  })

  it('keeps the persisted final assistant report over a stale checkpoint draft', async () => {
    apiMocks.getSession.mockResolvedValue({
      data: {
        messages: [
          { role: 'user', content: 'Completed research question' },
          {
            role: 'assistant',
            content: 'Persisted final revision',
            references_data: {
              references: [
                {
                  title: 'Persisted final source',
                  link: 'https://final.example/source',
                },
              ],
            },
          },
        ],
      },
    })
    apiMocks.getFullResearchCheckpoint.mockResolvedValue({
      data: {
        success: true,
        checkpoint: {
          id: 'checkpoint-1',
          session_id: 'session-1',
          query: 'Completed research question',
          phase: 'reviewing',
          status: 'completed',
          final_report: 'Stale pre-review draft',
          state_json: { references: [], charts: [] },
          ui_state_json: {
            research_steps: [],
            search_results: [],
            charts: [],
            knowledge_graph: null,
            streaming_report: 'Stale pre-review draft',
            references: [
              {
                title: 'Stale checkpoint source',
                link: 'https://stale.example/source',
                source: 'web',
              },
            ],
          },
        },
      },
    })

    renderPage()

    expect(await screen.findByText('Persisted final revision')).toBeInTheDocument()
    expect(screen.queryByText('Stale pre-review draft')).not.toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'Persisted final source' }),
    ).toHaveAttribute('href', 'https://final.example/source')
    expect(
      screen.queryByRole('link', { name: 'Stale checkpoint source' }),
    ).not.toBeInTheDocument()
    expect(apiMocks.addMessage).not.toHaveBeenCalled()
    expect(apiMocks.resumeResearch).not.toHaveBeenCalled()
  })

  it('waits for user-only history when the checkpoint response arrives first', async () => {
    let resolveSession!: (value: unknown) => void
    const sessionResponse = new Promise((resolve) => {
      resolveSession = resolve
    })
    apiMocks.getSession.mockReturnValue(sessionResponse)
    apiMocks.getFullResearchCheckpoint.mockResolvedValue({
      data: {
        success: true,
        checkpoint: {
          id: 'checkpoint-1',
          session_id: 'session-1',
          query: 'Checkpoint-first question',
          phase: 'completed',
          status: 'completed',
          final_report: 'Checkpoint-first final report',
          state_json: { references: [], charts: [] },
          ui_state_json: {
            research_steps: [],
            search_results: [],
            charts: [],
            knowledge_graph: null,
            streaming_report: 'Checkpoint-first final report',
          },
        },
      },
    })

    renderPage()
    await waitFor(() =>
      expect(apiMocks.getFullResearchCheckpoint).toHaveBeenCalled(),
    )
    resolveSession({
      data: {
        messages: [{ role: 'user', content: 'Checkpoint-first question' }],
      },
    })

    expect(await screen.findByText('Checkpoint-first final report')).toBeInTheDocument()
    expect(apiMocks.addMessage).toHaveBeenCalledTimes(1)
  })

  it('creates a new assistant after the latest user turn in multi-turn history', async () => {
    apiMocks.getSession.mockResolvedValue({
      data: {
        messages: [
          { role: 'user', content: 'Earlier question' },
          { role: 'assistant', content: 'Earlier answer' },
          { role: 'user', content: 'Latest research question' },
        ],
      },
    })
    apiMocks.getFullResearchCheckpoint.mockResolvedValue({
      data: {
        success: true,
        checkpoint: {
          id: 'checkpoint-1',
          session_id: 'session-1',
          query: 'Latest research question',
          phase: 'completed',
          status: 'completed',
          final_report: 'Latest durable report',
          state_json: { references: [], charts: [] },
          ui_state_json: {
            research_steps: [],
            search_results: [],
            charts: [],
            knowledge_graph: null,
            streaming_report: 'Latest durable report',
          },
        },
      },
    })

    renderPage()

    expect(await screen.findByText('Earlier answer')).toBeInTheDocument()
    expect(await screen.findByText('Latest durable report')).toBeInTheDocument()
    expect(apiMocks.addMessage).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({
        role: 'assistant',
        content: 'Latest durable report',
      }),
    )
  })

  it('ignores a delayed previous session while the new session is loading', async () => {
    const user = userEvent.setup()
    let resolveSessionOne!: (value: unknown) => void
    let resolveSessionTwo!: (value: unknown) => void
    const sessionOneResponse = new Promise((resolve) => {
      resolveSessionOne = resolve
    })
    const sessionTwoResponse = new Promise((resolve) => {
      resolveSessionTwo = resolve
    })
    apiMocks.getSession.mockImplementation((sessionId: string) =>
      sessionId === 'session-1' ? sessionOneResponse : sessionTwoResponse,
    )
    apiMocks.getFullResearchCheckpoint.mockImplementation((sessionId: string) =>
      Promise.resolve({
        data: {
          success: true,
          checkpoint: {
            id: `checkpoint-${sessionId}`,
            session_id: sessionId,
            query: `${sessionId} question`,
            phase: 'completed',
            status: 'completed',
            final_report: `${sessionId} final report`,
            state_json: { references: [], charts: [] },
            ui_state_json: {
              research_steps: [],
              search_results: [],
              charts: [],
              knowledge_graph: null,
              streaming_report: `${sessionId} final report`,
            },
          },
        },
      }),
    )

    renderPageWithSessionSwitcher()
    await waitFor(() =>
      expect(apiMocks.getFullResearchCheckpoint).toHaveBeenCalledWith('session-1'),
    )
    await user.click(screen.getByRole('button', { name: 'Switch session' }))
    await waitFor(() =>
      expect(apiMocks.getFullResearchCheckpoint).toHaveBeenCalledWith('session-2'),
    )

    await act(async () => {
      resolveSessionOne({
        data: { messages: [{ role: 'user', content: 'Late session-1 history' }] },
      })
      await Promise.resolve()
    })
    expect(screen.queryByText('Late session-1 history')).not.toBeInTheDocument()
    expect(screen.queryByText('session-2 final report')).not.toBeInTheDocument()

    await act(async () => {
      resolveSessionTwo({
        data: { messages: [{ role: 'user', content: 'session-2 question' }] },
      })
    })

    expect(await screen.findByText('session-2 final report')).toBeInTheDocument()
    expect(screen.queryByText('session-1 final report')).not.toBeInTheDocument()
  })

  it('ignores a delayed resume stream after switching sessions', async () => {
    const user = userEvent.setup()
    let resolveResume!: (value: unknown) => void
    const resumeResponse = new Promise((resolve) => {
      resolveResume = resolve
    })
    apiMocks.resumeResearch.mockReturnValue(resumeResponse)
    apiMocks.getSession.mockImplementation((sessionId: string) =>
      Promise.resolve({
        data: {
          messages:
            sessionId === 'session-1'
              ? [{ role: 'user', content: 'Running session question' }]
              : [{ role: 'user', content: 'New session question' }],
        },
      }),
    )
    apiMocks.getFullResearchCheckpoint.mockImplementation((sessionId: string) =>
      Promise.resolve(
        sessionId === 'session-1'
          ? {
              data: {
                success: true,
                checkpoint: {
                  id: 'checkpoint-running',
                  session_id: sessionId,
                  query: 'Running session question',
                  phase: 'reviewing',
                  status: 'running',
                  final_report: 'Running checkpoint draft',
                  state_json: { references: [], charts: [] },
                  ui_state_json: {
                    research_steps: [],
                    search_results: [],
                    charts: [],
                    knowledge_graph: null,
                    streaming_report: 'Running checkpoint draft',
                  },
                },
              },
            }
          : { data: { success: false } },
      ),
    )

    renderPageWithSessionSwitcher()
    await waitFor(() => expect(apiMocks.resumeResearch).toHaveBeenCalledWith('session-1'))
    await user.click(screen.getByRole('button', { name: 'Switch session' }))
    expect(await screen.findByText('New session question')).toBeInTheDocument()

    await act(async () => {
      resolveResume({
        data: eventStream([
          {
            type: 'research_step',
            content: {
              step_id: 'writing',
              step_type: 'writing',
              title: 'Old session writing',
              status: 'running',
            },
          },
          { type: 'research_complete', final_report: 'Old resumed final report' },
        ]),
      })
    })

    expect(screen.queryByText('Old resumed final report')).not.toBeInTheDocument()
    expect(screen.queryByText('Running checkpoint draft')).not.toBeInTheDocument()
    expect(apiMocks.addMessage).not.toHaveBeenCalled()
  })

  // P-15：本地知识库模式必须把 kb_name 带下去 —— 后端 `Scout._execute_local_search`
  // 在 kb_name 为空时会「静默跳过」本地检索（零结果且无报错），用户勾了模式却什么都没搜。
  it('forwards the selected knowledge base when local search is enabled', async () => {
    deviceMocks.state.searchModes = ['local']
    deviceMocks.state.kbName = 'demo'
    const user = userEvent.setup()

    renderPage()
    await user.click(screen.getByRole('button', { name: 'Start research' }))

    await waitFor(() =>
      expect(apiMocks.deepsearch).toHaveBeenCalledWith(
        expect.objectContaining({ search_modes: ['local'], kb_name: 'demo' }),
        expect.anything(),
      ),
    )
  })

  it('omits kb_name when local search is disabled', async () => {
    deviceMocks.state.searchModes = ['web']
    deviceMocks.state.kbName = 'demo'  // 残留的旧选择不应被带出去
    const user = userEvent.setup()

    renderPage()
    await user.click(screen.getByRole('button', { name: 'Start research' }))

    await waitFor(() => expect(apiMocks.deepsearch).toHaveBeenCalled())
    expect(apiMocks.deepsearch).toHaveBeenCalledWith(
      expect.objectContaining({ kb_name: undefined }),
      expect.anything(),
    )
  })
})
