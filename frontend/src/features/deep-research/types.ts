export type ResearchPhase =
  | 'architecting'
  | 'awaiting_outline_approval'
  | 'planning'
  | 'researching'
  | 'analyzing'
  | 'writing'
  | 'reviewing'
  | 'completed'

export type OutlineSectionType = 'qualitative' | 'quantitative' | 'mixed'

export interface OutlineSection {
  id: string
  title: string
  description: string
  section_type: OutlineSectionType
  requires_data: boolean
  requires_chart: boolean
}

export interface ResearchQuestion {
  id: string
  text: string
}

export interface EditableResearchPlan {
  sections: OutlineSection[]
  researchQuestions: ResearchQuestion[]
}

export interface OutlinePendingApprovalEvent {
  type: 'outline_pending_approval'
  session_id: string
  outline_revision: string
  sections: OutlineSection[]
  research_questions: ResearchQuestion[]
}

/** 大纲事件里的章节（后端键集与 `OutlineSection` 略有出入，均为可选）。 */
export interface OutlineSectionPayload {
  id?: string
  title?: string
  description?: string
  section_type?: OutlineSectionType
  requires_data?: boolean
  requires_chart?: boolean
}

export interface SearchResultPayload {
  id?: string
  title?: string
  source?: string
  date?: string
  url?: string
  snippet?: string
}

export interface ChartPayload {
  /** 后端用 `chart_type`，也有旧事件直接给 `type`。 */
  chart_type?: string
  type?: string
  title?: string
  subtitle?: string
  echarts_option?: Record<string, unknown>
  image?: string
  image_base64?: string
  data?: unknown
}

export interface ReferencePayload {
  title?: string
  source_name?: string
  url?: string
  source_url?: string
  content?: string
  summary?: string
  source?: string
  source_type?: string
}

export interface ResearchStepPayload {
  step_type?: string
  status?: string
  title?: string
  subtitle?: string
  stats?: Record<string, number | undefined>
}

export interface KnowledgeGraphPayload {
  graph?: {
    nodes?: unknown[]
    edges?: unknown[]
    stats?: Record<string, number | undefined>
  }
  nodes?: unknown[]
  edges?: unknown[]
  stats?: Record<string, number | undefined>
}

export interface OutlinePayload {
  outline?: OutlineSectionPayload[]
  research_questions?: (string | ResearchQuestion)[]
}

export interface ObservationPayload {
  agent?: string
  section?: string
  facts_count?: number
  data_points_count?: number
  duplicates_removed?: number
  insights?: string[]
  source_quality?: string
  search_results?: unknown[]
  extracted_facts?: unknown[]
  data_points?: unknown[]
}

export interface ActionPayload {
  tool?: string
  queries?: string[]
  section?: string
}

export interface SectionContentPayload {
  section_id?: string
  section_title?: string
  content?: string
  key_points?: string[]
}

export interface SectionDraftPayload {
  section_title?: string
  word_count?: number
  key_points?: string[]
}

export interface ReportDraftPayload {
  content?: string
  word_count?: number
  references_count?: number
}

export interface ReviewPayload {
  quality_score?: number
  passed?: boolean
  verdict?: string
}

export interface RevisionCompletePayload {
  changes_count?: number
}

export interface StockQuotePayload {
  code?: string
  name?: string
  price?: string | number
  change?: string | number
  change_percent?: string
  high?: string
  low?: string
  volume?: string
  turnover?: string
  open?: string
  prev_close?: string
}

export interface ReferenceMaterialPayload {
  reference_id?: number
  name?: string
  url?: string
  summary?: string
  source?: string
}

export interface ResearchStartEvent {
  type: 'research_start'
  query?: string
  mode?: string
}

export interface ResearchResumedEvent {
  type: 'research_resumed'
  mode?: string
}

export interface ResearchCompleteEvent {
  type: 'research_complete'
  final_report?: string
  references?: ReferencePayload[]
  mode?: string
}

export interface ResearchCancelledEvent {
  type: 'research_cancelled'
}

export interface ResearchStepEvent {
  type: 'research_step'
  content?: ResearchStepPayload
}

export interface SearchResultsEvent {
  type: 'search_results'
  content?: {
    results?: SearchResultPayload[]
    isIncremental?: boolean
  }
  /** 兼容事件把查询文本放在顶层。 */
  subquery?: string
  count?: number
}

export interface KnowledgeGraphEvent {
  type: 'knowledge_graph'
  content?: KnowledgeGraphPayload
}

export interface ChartsEvent {
  type: 'charts'
  content?: { charts?: ChartPayload[] }
}

export interface ChartEvent {
  type: 'chart'
  content?: ChartPayload
}

export interface DataInsightEvent {
  type: 'data_insight'
  insights?: string[]
}

export interface PhaseStreamEvent {
  type: 'phase'
  phase?: string
  content?: unknown
}

export interface OutlineStreamEvent {
  type: 'outline'
  content?: OutlinePayload
}

export interface PlanEvent {
  type: 'plan'
  understanding?: string
  strategy?: string
  sub_queries?: { query?: string; purpose?: string; tool?: string }[]
  expected_aspects?: string[]
}

export interface ReactStartEvent {
  type: 'react_start'
  mode?: string
}

export interface ThoughtEvent {
  type: 'thought'
  step?: number
  content?: unknown
}

export interface ActionEvent {
  type: 'action'
  step?: number
  tool?: string
  params?: Record<string, unknown>
  content?: ActionPayload
}

export interface ObservationEvent {
  type: 'observation'
  step?: number
  tool?: string
  /** 旧事件叫 `queries_executed`；新事件把查询挂在 `queries`。 */
  queries_executed?: string[]
  queries?: string[]
  success?: boolean
  result?: unknown
  content?: unknown
}

export interface SectionDraftEvent {
  type: 'section_draft'
  content?: SectionDraftPayload
}

export interface SectionContentEvent {
  type: 'section_content'
  content?: SectionContentPayload
}

export interface ReportDraftEvent {
  type: 'report_draft'
  content?: string | ReportDraftPayload
}

export interface ReviewEvent {
  type: 'review'
  content?: ReviewPayload
}

export interface RevisionCompleteEvent {
  type: 'revision_complete'
  content?: RevisionCompletePayload
}

export interface ErrorEvent {
  type: 'error'
  content?: unknown
}

export interface StockQuoteEvent {
  type: 'stock_quote'
  content?: StockQuotePayload
}

export interface ThinkingStepEvent {
  type: 'status' | 'thinking_step'
  subquery?: string
  content?: string
  count?: number
}

export interface SearchResultItemEvent {
  type: 'search_result_item'
  result?: {
    id?: string
    subquery?: string
    url?: string
    name?: string
    summary?: string
    snippet?: string
    siteName?: string
    siteIcon?: string
  }
}

export interface ThinkingEvent {
  type: 'thinking'
  content?: string
}

export interface AnswerEvent {
  type: 'answer' | 'final_answer'
  content?: string
}

export interface ReferenceMaterialsEvent {
  type: 'reference_materials'
  content?: ReferenceMaterialPayload[]
}

/** 前端尚未消费、但确实会到达的事件（类型已声明，行为为 no-op）。 */
export type UnhandledResearchEventType =
  | 'outline_revision'
  | 'outline_approval'
  | 'planning_error'
  | 'checkpoint_saved'
  | 'search_progress'
  | 'critic_feedback'
  | 'keywords_generated'
  | 'code'
  | 'code_result'
  | 'code_fix'
  | 'warning'

export interface UnhandledResearchEvent {
  type: UnhandledResearchEventType
  content?: unknown
}

/**
 * 普通聊天流事件：没有 `type` 判别式，只带 `content` / `thinking` / `documents` /
 * `image_results`（`index.tsx` 的 else 分支）。
 */
export interface LegacyChatEvent {
  type?: undefined
  content?: string
  thinking?: boolean
  documents?: unknown[]
  image_results?: unknown
  role?: string
}

/**
 * 线事件（SSE `data:` 后的 JSON）到 UI 状态的输入契约。
 *
 * `type` 判别式补齐到后端真实事件集合；`LegacyChatEvent` 保留「无 type 的普通聊天事件」
 * 这一历史形态（`research-stream.test.ts` 钉住）。两者之外的形状不再可能，故调用方
 * 无需 `any` 强转。
 */
export type ResearchEvent =
  | OutlinePendingApprovalEvent
  | ResearchStartEvent
  | ResearchResumedEvent
  | ResearchCompleteEvent
  | ResearchCancelledEvent
  | ResearchStepEvent
  | SearchResultsEvent
  | KnowledgeGraphEvent
  | ChartsEvent
  | ChartEvent
  | DataInsightEvent
  | PhaseStreamEvent
  | OutlineStreamEvent
  | PlanEvent
  | ReactStartEvent
  | ThoughtEvent
  | ActionEvent
  | ObservationEvent
  | SectionDraftEvent
  | SectionContentEvent
  | ReportDraftEvent
  | ReviewEvent
  | RevisionCompleteEvent
  | ErrorEvent
  | StockQuoteEvent
  | ThinkingStepEvent
  | SearchResultItemEvent
  | ThinkingEvent
  | AnswerEvent
  | ReferenceMaterialsEvent
  | UnhandledResearchEvent
  | LegacyChatEvent
