import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import DiagnosticsTimeline from './diagnostics-timeline'

describe('DiagnosticsTimeline', () => {
  it('renders the latest run identifiers, usage, and chronological events', () => {
    render(
      <DiagnosticsTimeline
        loading={false}
        timeline={{
          runs: [
            {
              run_id: 'run-12345678',
              research_id: 'research-12345678',
              session_id: 'session-1',
              request_id: 'request-12345678',
              trace_id: 'trace-12345678',
              status: 'completed',
              current_phase: 'writing',
              started_at: '2026-07-19T10:00:00Z',
              ended_at: '2026-07-19T10:00:02Z',
              duration_ms: 2000,
              input_tokens: 120,
              output_tokens: 80,
            },
          ],
          events: [
            {
              id: 1,
              run_id: 'run-12345678',
              sequence: 1,
              event_type: 'research.started',
              phase: 'planning',
              status: 'info',
              payload: {},
              duration_ms: 15,
              trace_id: 'trace-12345678',
              created_at: '2026-07-19T10:00:00Z',
            },
            {
              id: 2,
              run_id: 'run-12345678',
              sequence: 2,
              event_type: 'research.completed',
              phase: 'writing',
              status: 'success',
              payload: {},
              duration_ms: 2000,
              trace_id: 'trace-12345678',
              created_at: '2026-07-19T10:00:02Z',
            },
          ],
          next_cursor: null,
        }}
      />,
    )

    expect(screen.getByText('已完成')).toBeInTheDocument()
    expect(screen.getByText('200 Token')).toBeInTheDocument()
    expect(screen.getByText('trace-12345678')).toBeInTheDocument()
    expect(screen.getByText('研究开始')).toBeInTheDocument()
    expect(screen.getByText('研究完成')).toBeInTheDocument()
  })

  it('shows a durable empty state when a session has no runs yet', () => {
    render(
      <DiagnosticsTimeline
        loading={false}
        timeline={{ runs: [], events: [], next_cursor: null }}
      />,
    )

    expect(screen.getByText('暂无研究运行记录')).toBeInTheDocument()
  })
})
