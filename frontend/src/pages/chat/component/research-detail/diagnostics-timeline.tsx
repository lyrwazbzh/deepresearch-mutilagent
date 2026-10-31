import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import type {
  ResearchEventDiagnostic,
  ResearchTimeline,
} from '@/api/session'

import styles from './diagnostics-timeline.module.scss'

interface DiagnosticsTimelineProps {
  timeline?: ResearchTimeline | null
  loading: boolean
  error?: string
  onRefresh?: () => void
}

const eventLabels: Record<string, string> = {
  'research.started': '研究开始',
  'research.completed': '研究完成',
  'research.failed': '研究失败',
  'research.paused': '等待用户操作',
  'research.cancelled': '研究已取消',
  'llm.completed': '模型调用完成',
  'llm.failed': '模型调用失败',
  'retrieval.completed': '检索完成',
  'retrieval.failed': '检索失败',
  'tool.completed': '工具调用完成',
  'tool.failed': '工具调用失败',
}

const statusLabels: Record<string, string> = {
  running: '运行中',
  completed: '已完成',
  paused: '已暂停',
  cancelled: '已取消',
  failed: '失败',
}

function formatDuration(duration?: number | null): string {
  if (duration == null) return '—'
  if (duration < 1000) return `${duration} ms`
  return `${(duration / 1000).toFixed(duration < 10_000 ? 1 : 0)} s`
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function eventIcon(event: ResearchEventDiagnostic) {
  if (event.status === 'error' || event.event_type.endsWith('.failed')) {
    return <CloseCircleOutlined />
  }
  if (event.status === 'success' || event.event_type.endsWith('.completed')) {
    return <CheckCircleOutlined />
  }
  return <ClockCircleOutlined />
}

export default function DiagnosticsTimeline({
  timeline,
  loading,
  error,
  onRefresh,
}: DiagnosticsTimelineProps) {
  const latestRun = timeline?.runs[0]
  const events = latestRun
    ? (timeline?.events ?? []).filter((event) => event.run_id === latestRun.run_id)
    : []

  if (loading && !timeline) {
    return <div className={styles.state}>正在读取诊断记录…</div>
  }

  if (!latestRun) {
    return (
      <div className={styles.state}>
        <ExperimentOutlined />
        <strong>{error || '暂无研究运行记录'}</strong>
        <span>开始一次深度研究后，这里会持久保存完整阶段时间线。</span>
        {onRefresh && (
          <button type="button" onClick={onRefresh}>
            <ReloadOutlined /> 重新加载
          </button>
        )}
      </div>
    )
  }

  const totalTokens = latestRun.input_tokens + latestRun.output_tokens
  const langfuseUrl = import.meta.env.VITE_LANGFUSE_URL || 'http://127.0.0.1:3000'
  const grafanaUrl = import.meta.env.VITE_GRAFANA_URL || 'http://127.0.0.1:3002'
  const kibanaUrl = import.meta.env.VITE_KIBANA_URL || 'http://127.0.0.1:5601'

  return (
    <div className={styles.root}>
      <section className={styles.summary}>
        <div className={styles.summaryHeader}>
          <div>
            <span className={`${styles.status} ${styles[latestRun.status] || ''}`}>
              {statusLabels[latestRun.status] || latestRun.status}
            </span>
            <strong>最近一次研究运行</strong>
          </div>
          {onRefresh && (
            <button type="button" onClick={onRefresh} aria-label="刷新诊断记录">
              <ReloadOutlined spin={loading} />
            </button>
          )}
        </div>
        <div className={styles.metrics}>
          <div><span>总耗时</span><strong>{formatDuration(latestRun.duration_ms)}</strong></div>
          <div><span>Token</span><strong>{totalTokens} Token</strong></div>
          <div><span>当前阶段</span><strong>{latestRun.current_phase || '—'}</strong></div>
        </div>
        <dl className={styles.identifiers}>
          <div><dt>run_id</dt><dd>{latestRun.run_id}</dd></div>
          <div><dt>research_id</dt><dd>{latestRun.research_id}</dd></div>
          {latestRun.request_id && <div><dt>request_id</dt><dd>{latestRun.request_id}</dd></div>}
          {latestRun.trace_id && <div><dt>trace_id</dt><dd>{latestRun.trace_id}</dd></div>}
        </dl>
        {latestRun.error_summary && (
          <div className={styles.error}>{latestRun.error_summary}</div>
        )}
        <div className={styles.links}>
          <a href={langfuseUrl} target="_blank" rel="noreferrer">Langfuse 链路</a>
          <a href={`${grafanaUrl}/d/deep-research-overview/deep-research-overview`} target="_blank" rel="noreferrer"><DashboardOutlined /> Grafana 指标</a>
          <a href={kibanaUrl} target="_blank" rel="noreferrer">Kibana 日志</a>
        </div>
      </section>

      <section className={styles.timeline} aria-label="研究事件时间线">
        {events.map((event) => (
          <article key={event.id} className={styles.event}>
            <div className={`${styles.eventIcon} ${event.status === 'error' ? styles.eventError : ''}`}>
              {eventIcon(event)}
            </div>
            <div className={styles.eventBody}>
              <div>
                <strong>{eventLabels[event.event_type] || event.event_type}</strong>
                <time>{formatTime(event.created_at)}</time>
              </div>
              <p>
                {event.phase || 'system'}
                {event.duration_ms != null && ` · ${formatDuration(event.duration_ms)}`}
              </p>
            </div>
          </article>
        ))}
        {events.length === 0 && <div className={styles.noEvents}>本次运行暂无阶段事件</div>}
      </section>
    </div>
  )
}
