import { beforeEach, describe, expect, it, vi } from 'vitest'

const requestMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('./request', () => ({ request: requestMocks }))

import { deepsearch, getFullResearchCheckpoint } from './session'

describe('deep research request configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('treats a missing optional checkpoint as a silent probe', () => {
    getFullResearchCheckpoint('session-1')

    expect(requestMocks.get).toHaveBeenCalledWith(
      '/research/checkpoint/session-1/full',
      { loading: false, errorToast: false },
    )
  })

  it('lets the chat page handle research startup failures', () => {
    deepsearch({ query: 'Huawei outlook', session_id: 'session-1' })

    expect(requestMocks.post).toHaveBeenCalledWith(
      '/research/stream',
      expect.any(Object),
      expect.objectContaining({ errorToast: false }),
    )
  })

  // P-15：本地知识库模式必须把 kb_name 发出去，否则后端会静默跳过本地检索
  it('forwards the selected knowledge base for local search', () => {
    deepsearch({
      query: 'Huawei outlook',
      session_id: 'session-1',
      search_modes: ['local'],
      kb_name: 'demo',
    })

    expect(requestMocks.post).toHaveBeenCalledWith(
      '/research/stream',
      expect.objectContaining({ search_modes: ['local'], kb_name: 'demo' }),
      expect.any(Object),
    )
  })

  it('omits kb_name when only web search is requested', () => {
    deepsearch({ query: 'Huawei outlook', session_id: 'session-1', search_modes: ['web'] })

    const body = requestMocks.post.mock.calls[0][1]
    expect(body).not.toHaveProperty('kb_name')
  })
})
