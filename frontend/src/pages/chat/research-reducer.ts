/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { ChatType } from '@/configs'
import type {
  OutlinePendingApprovalEvent,
  ReportDraftPayload,
  ResearchEvent,
} from '@/features/deep-research/types'
import type {
  ChartConfig,
  KnowledgeGraphData,
  ResearchDetailData,
  ResearchStep,
} from '@/pages/chat/component/research-detail'
import type { StepDetailData } from '@/pages/chat/component/step-detail-panel'
import { uniqueId } from 'lodash-es'

/**
 * 「线事件 → UI 状态」的纯归约（T66）。
 *
 * 此前这段映射（约 760 行、30 个事件分支）长在 chat 页面的 `parseData` 里，直接读写
 * 12 个 `useState` / `useRef` / valtio setter —— 要驱动**一条**事件分支，测试得 mock
 * 14 个业务 module。抽取后 `reduceResearchEvent(state, event) => state` 不触碰 React，
 * 单测只需喂事件、断言状态。
 *
 * 页面侧只做「订阅」：把返回状态的各字段写回 ref 与 setState。
 *
 * 注意代码风格：本模块刻意保留原 `parseData` 的分支顺序与表达式形态（含「多个独立
 * `if` 可对同一事件同时命中」与「`else if` 链」的差异），以免行为漂移；此处只做
 * 「突变 → 不可变」与「`any` → 真实事件 union」两件事。
 */
export interface ResearchUiState {
  /** 会话项（会话列表里的那一条助手消息）。 */
  chatItem: API.ChatItem
  /** 研究步骤条。 */
  researchSteps: ResearchStep[]
  /** 步骤详情（按 stepType 索引）。 */
  researchDetails: Map<string, ResearchDetailData>
  /** 当前选中的研究详情（右侧面板）。 */
  selectedResearchDetail: ResearchDetailData | null
  /** 旧版步骤详情（按 stepId 索引）。 */
  stepDetails: Map<string, StepDetailData>
  /** 当前选中的旧版步骤详情。 */
  selectedStepDetail: StepDetailData | null
  /** 待审批大纲。 */
  pendingOutline: OutlinePendingApprovalEvent | null
  /** 大纲审批错误。 */
  outlineApprovalError: string | undefined
  /** 「ref 内容变了」的显式失效信号（见 `aggregatedResearchData`）。 */
  researchDataVersion: number
}

/**
 * 建初值。流开始前页面的 refs 已经装好（检查点恢复路径会先填 refs 再开流），
 * 故这里从 refs 的现值播种，保证恢复态与流事件落在同一份状态上。
 */
export function createResearchUiState(seed: {
  chatItem: API.ChatItem
  researchSteps?: ResearchStep[]
  researchDetails?: Map<string, ResearchDetailData>
  stepDetails?: Map<string, StepDetailData>
  selectedResearchDetail?: ResearchDetailData | null
  selectedStepDetail?: StepDetailData | null
  pendingOutline?: OutlinePendingApprovalEvent | null
  outlineApprovalError?: string
  researchDataVersion?: number
}): ResearchUiState {
  return {
    chatItem: seed.chatItem,
    researchSteps: seed.researchSteps ?? [],
    researchDetails: seed.researchDetails ?? new Map(),
    selectedResearchDetail: seed.selectedResearchDetail ?? null,
    selectedStepDetail: seed.selectedStepDetail ?? null,
    stepDetails: seed.stepDetails ?? new Map(),
    pendingOutline: seed.pendingOutline ?? null,
    outlineApprovalError: seed.outlineApprovalError,
    researchDataVersion: seed.researchDataVersion ?? 0,
  }
}

/** V2 事件的 `content` 可能是字符串、也可能再包一层 `{ content }`。 */
function extractContent(data: unknown): string {
  if (typeof data === 'string') return data
  if (data !== null && typeof data === 'object') {
    const record = data as Record<string, unknown>
    if (typeof record.content === 'string') return record.content
    return JSON.stringify(data, null, 2)
  }
  return String(data || '')
}

/**
 * 复制出可安全突变的草稿：`reduceResearchEvent` 对外保持不可变（入参绝不被写），
 * 对内沿用原实现的「就地赋值 / push」写法 —— 只把会被写到的容器换成副本。
 */
function draft(state: ResearchUiState): ResearchUiState {
  const researchDetails = new Map<string, ResearchDetailData>()
  state.researchDetails.forEach((detail, key) => {
    researchDetails.set(key, {
      ...detail,
      searchResults: detail.searchResults
        ? [...detail.searchResults]
        : detail.searchResults,
      charts: detail.charts ? [...detail.charts] : detail.charts,
      sections: detail.sections ? [...detail.sections] : detail.sections,
    })
  })

  const stepDetails = new Map<string, StepDetailData>()
  state.stepDetails.forEach((detail, key) =>
    stepDetails.set(key, { ...detail }),
  )

  const chatItem: API.ChatItem = {
    ...state.chatItem,
    reactSteps: state.chatItem.reactSteps
      ? [...state.chatItem.reactSteps]
      : state.chatItem.reactSteps,
    charts: state.chatItem.charts
      ? [...state.chatItem.charts]
      : state.chatItem.charts,
    insights: state.chatItem.insights
      ? [...state.chatItem.insights]
      : state.chatItem.insights,
    reference: state.chatItem.reference
      ? [...state.chatItem.reference]
      : state.chatItem.reference,
    search_results: state.chatItem.search_results
      ? [...state.chatItem.search_results]
      : state.chatItem.search_results,
    thinks: state.chatItem.thinks
      ? state.chatItem.thinks.map((think) => ({
          ...think,
          results: think.results ? [...think.results] : think.results,
        }))
      : state.chatItem.thinks,
  }

  return {
    ...state,
    chatItem,
    researchSteps: [...state.researchSteps],
    researchDetails,
    stepDetails,
  }
}

export function reduceResearchEvent(
  state: ResearchUiState,
  event: ResearchEvent,
): ResearchUiState {
  const next = draft(state)
  if (next.chatItem.type === ChatType.Deepsearch) {
    reduceDeepsearchEvent(next, event)
  } else {
    // 非深研会话走普通聊天流：事件没有 `type`（见 research-stream.test.ts 的兼容用例）
    reduceLegacyChatEvent(
      next,
      event as Extract<ResearchEvent, { type?: undefined }>,
    )
  }
  return next
}

/** 深研事件：30 个分支的映射。 */
function reduceDeepsearchEvent(d: ResearchUiState, json: ResearchEvent): void {
  const target = d.chatItem

  if (json.type === 'outline_pending_approval') {
    d.pendingOutline = json
    d.outlineApprovalError = undefined
    target.loading = false
    return
  }

  // V2 研究开始事件
  if (json.type === 'research_start') {
    d.pendingOutline = null
    d.outlineApprovalError = undefined
    target.reactMode = true
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    target.reactSteps.push({
      step: 0,
      type: 'plan',
      content: `🔬 开始深度研究: ${json.query || ''}`,
      timestamp: Date.now(),
    })
    // 重置研究步骤
    d.researchSteps = []
    d.researchDetails = new Map()
    d.selectedResearchDetail = null
    d.researchDataVersion = 0
  }

  // V2 研究步骤事件
  if (json.type === 'research_step') {
    const content = json.content || {}
    const stepType = content.step_type as ResearchStep['type']

    // 转换 stats 从 snake_case 到 camelCase
    const rawStats = content.stats || {}
    // 原实现把缺失的计数写成 `undefined`（后端 stats 键集不固定），此处保持值不变，
    // 仅收敛到 `ResearchStep['stats']`。
    const stats = {
      resultsCount: rawStats.results_count,
      chartsCount: rawStats.charts_count,
      entitiesCount: rawStats.entities_count,
      sectionsCount: rawStats.sections_count,
      wordCount: rawStats.word_count,
      questionsCount: rawStats.questions_count,
      sourcesCount: rawStats.sources_count,
      referencesCount: rawStats.references_count,
    } as ResearchStep['stats']

    const prev = d.researchSteps
    if (prev.find((s) => s.type === stepType)) {
      // 更新现有步骤 - 保持 id 为 stepType
      d.researchSteps = prev.map((s) =>
        s.type === stepType
          ? { ...s, status: content.status as ResearchStep['status'], stats }
          : s,
      )
    } else {
      // 添加新步骤 - 使用 stepType 作为 id
      d.researchSteps = [
        ...prev,
        {
          id: stepType, // 使用 stepType 作为 id，与 detail key 保持一致
          type: stepType,
          title: content.title || stepType,
          subtitle: content.subtitle || '',
          status: (content.status || 'running') as ResearchStep['status'],
          stats,
        },
      ]
    }

    // 初始化详情数据 - 使用 stepType 作为 key
    if (!d.researchDetails.has(stepType)) {
      const newDetail: ResearchDetailData = {
        stepId: stepType, // 使用类型作为 ID
        stepType,
        title: content.title || stepType,
        subtitle: content.subtitle,
        searchResults: [],
        charts: [],
      }
      d.researchDetails.set(stepType, newDetail)
      // 自动选中新的步骤详情（特别是 searching/researching 步骤）
      if (
        stepType === 'searching' ||
        stepType === 'researching' ||
        content.status === 'running'
      ) {
        d.selectedResearchDetail = { ...newDetail }
      }
    }
  }

  // V2 搜索结果事件 (详情面板用)
  if (json.type === 'search_results') {
    const content = json.content || {}
    const results = content.results || []
    const isIncremental = content.isIncremental || false
    // 使用 stepType 作为 key 查找 detail
    const searchingType = d.researchSteps.find((s) => s.type === 'searching')
      ? 'searching'
      : 'researching'
    const detail = d.researchDetails.get(searchingType)
    if (detail) {
      const newResults = results.map((r, i) => ({
        id: (r.id as string) || `sr_${Date.now()}_${i}`,
        title: r.title as string,
        source: r.source as string,
        date: r.date as string,
        url: r.url as string,
        snippet: r.snippet as string,
      }))
      // 增量模式：累加结果；否则替换
      if (isIncremental && detail.searchResults) {
        detail.searchResults = [...detail.searchResults, ...newResults]
      } else {
        detail.searchResults = newResults
      }
      // 更新步骤统计
      d.researchSteps = d.researchSteps.map((s) =>
        s.type === searchingType
          ? {
              ...s,
              stats: {
                ...s.stats,
                resultsCount: detail.searchResults?.length || 0,
              },
            }
          : s,
      )
      // 自动选中并触发聚合数据更新
      d.selectedResearchDetail = { ...detail }
      d.researchDataVersion += 1
    }
  }

  // V2 知识图谱事件
  if (json.type === 'knowledge_graph') {
    const content = json.content || {}
    const graph = content.graph || content
    // 优先存储到 analyzing，其次 researching/searching - 使用 stepType 作为 key
    const targetType = d.researchDetails.has('analyzing')
      ? 'analyzing'
      : d.researchDetails.has('researching')
        ? 'researching'
        : 'searching'
    const detail = d.researchDetails.get(targetType)
    if (detail) {
      detail.knowledgeGraph = {
        nodes: (graph.nodes || []) as KnowledgeGraphData['nodes'],
        edges: (graph.edges || []) as KnowledgeGraphData['edges'],
        stats: (content.stats || graph.stats) as KnowledgeGraphData['stats'],
      }
      d.selectedResearchDetail = { ...detail }
      d.researchDataVersion += 1
    } else {
      console.warn(
        `[前端] knowledge_graph: ⚠️ 未找到 detail, 可用 keys:`,
        Array.from(d.researchDetails.keys()),
      )
    }
  }

  // V2 图表事件 (DataAnalyst 发送的 ECharts 图表)
  if (json.type === 'charts') {
    const content = json.content || {}
    const charts = content.charts || []

    // 使用 stepType 作为 key 查找 detail
    const detail = d.researchDetails.get('analyzing')
    if (detail) {
      detail.charts = charts as ResearchDetailData['charts']
      // 更新步骤统计
      d.researchSteps = d.researchSteps.map((s) =>
        s.type === 'analyzing'
          ? { ...s, stats: { ...s.stats, chartsCount: charts.length } }
          : s,
      )
      d.selectedResearchDetail = { ...detail }
      d.researchDataVersion += 1
    }
    // 同时保存到 target.charts 供报告使用
    if (!target.charts) {
      target.charts = []
    }
    target.charts.push(...(charts as NonNullable<API.ChatItem['charts']>))
  }

  // V2 阶段切换事件
  if (json.type === 'phase') {
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    const phase = json.phase as string
    const phaseLabels: Record<string, string> = {
      planning: '📋 规划阶段',
      researching: '🔍 搜索阶段',
      analyzing: '📊 分析阶段',
      writing: '✍️ 写作阶段',
      reviewing: '🔎 审核阶段',
      re_researching: '🔄 补充搜索',
      rewriting: '📝 重写阶段',
      revising: '📝 修订阶段',
    }
    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'thought',
      content: `${phaseLabels[phase] || phase}: ${extractContent(json.content)}`,
      timestamp: Date.now(),
    })

    // 同时更新研究步骤条 - 映射 phase 到 step_type
    const phaseToStepType: Record<string, ResearchStep['type']> = {
      writing: 'writing',
      reviewing: 'reviewing',
      re_researching: 're_researching',
      rewriting: 'revising',
      revising: 'revising',
    }
    const stepType = phaseToStepType[phase]
    if (stepType && !d.researchSteps.find((s) => s.type === stepType)) {
      d.researchSteps = [
        ...d.researchSteps,
        {
          id: stepType, // 使用 stepType 作为 ID
          type: stepType,
          title: phaseLabels[phase] || phase,
          subtitle: extractContent(json.content) || '',
          status: 'running',
        },
      ]

      // 同时初始化 researchDetail - 使用 stepType 作为 key
      if (!d.researchDetails.has(stepType)) {
        const newDetail: ResearchDetailData = {
          stepId: stepType,
          stepType,
          title: phaseLabels[phase] || phase,
          subtitle: extractContent(json.content) || '',
          searchResults: [],
          charts: [],
          streamingReport: '',
        }
        d.researchDetails.set(stepType, newDetail)
        // 对于 writing 步骤，自动选中以便显示过程报告
        if (stepType === 'writing') {
          d.selectedResearchDetail = { ...newDetail }
        }
      }
    }
  }

  // V2 大纲事件
  if (json.type === 'outline') {
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    const outlineContent = json.content || {}
    const outline = outlineContent.outline || []
    const questions = outlineContent.research_questions || []

    let content = '**研究大纲**\n\n'
    if (outline.length > 0) {
      content += outline
        .map(
          (sec, i) => `${i + 1}. **${sec.title}**\n   ${sec.description || ''}`,
        )
        .join('\n\n')
    }
    if (questions.length > 0) {
      content +=
        '\n\n**核心问题**\n' +
        questions
          .map((q) => `• ${typeof q === 'string' ? q : q.text}`)
          .join('\n')
    }

    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'plan',
      content,
      timestamp: Date.now(),
    })
  }

  // V2 研究完成事件
  if (json.type === 'research_complete') {
    d.pendingOutline = null
    // 设置最终报告为内容
    if (json.final_report) {
      target.content = json.final_report

      // 同时存储到研究详情中供"过程报告"tab显示 - 使用 stepType 作为 key
      const writingType = d.researchDetails.has('writing')
        ? 'writing'
        : 'generating'
      const detail = d.researchDetails.get(writingType)
      if (detail) {
        detail.streamingReport = json.final_report
        d.selectedResearchDetail = { ...detail }
        d.researchDataVersion += 1
      }
    }
    // 设置引用
    if (json.references && json.references.length > 0) {
      target.reference = json.references.map((ref, i) => ({
        id: i + 1,
        title: (ref.title as string) || (ref.source_name as string) || '来源',
        link: (ref.url as string) || (ref.source_url as string) || '',
        content: (ref.content as string) || (ref.summary as string) || '',
        source: ref.source_type === 'local' ? 'knowledge' : 'web',
      }))
    }

    // 标记所有研究步骤为完成
    d.researchSteps = d.researchSteps.map((s) => ({
      ...s,
      status: 'completed' as const,
    }))
    // 确保触发重新计算
    d.researchDataVersion += 1
  }

  // 检测 ReAct 模式
  const mode = 'mode' in json ? json.mode : undefined
  if (mode === 'react' || mode === 'optimized' || json.type === 'react_start') {
    target.reactMode = true
  }

  // 研究计划事件 (V1)
  if (json.type === 'plan' && json.understanding) {
    target.researchPlan = {
      understanding: json.understanding || '',
      strategy: json.strategy || '',
      subQueries: (json.sub_queries || []).map((sq) => ({
        query: sq.query as string,
        purpose: sq.purpose as string,
        tool: sq.tool as string,
      })),
      expectedAspects: json.expected_aspects || [],
    }
    // 同时添加到 reactSteps 用于展示
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    target.reactSteps.push({
      step: 0,
      type: 'plan',
      content: `**研究计划**\n\n理解: ${json.understanding}\n\n策略: ${json.strategy}\n\n子查询:\n${(json.sub_queries || []).map((sq) => `• ${sq.query} (${sq.purpose})`).join('\n')}`,
      timestamp: Date.now(),
    })
  }

  // ReAct 事件处理 (兼容 V1 和 V2)
  if (json.type === 'thought') {
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    target.reactSteps.push({
      step: json.step || target.reactSteps.length + 1,
      type: 'thought',
      content: extractContent(json.content),
      timestamp: Date.now(),
    })
  } else if (json.type === 'action') {
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    // V2 格式的 action
    const actionContent = json.content || {}
    const tool = actionContent.tool || json.tool
    const isParallel = tool === 'parallel_search'
    const queries = actionContent.queries || []
    const section = actionContent.section || ''

    let displayContent = ''
    if (isParallel) {
      displayContent = `并行搜索${section ? ` (${section})` : ''} ${queries.length} 个查询:\n${queries.map((q) => `• ${q}`).join('\n')}`
    } else {
      displayContent = `调用工具: ${tool}${section ? ` - ${section}` : ''}`
    }

    target.reactSteps.push({
      step: json.step || target.reactSteps.length + 1,
      type: 'action',
      content: displayContent,
      tool: tool,
      params: json.params || (actionContent as Record<string, unknown>),
      queries: isParallel ? queries : undefined,
      timestamp: Date.now(),
    })
  } else if (json.type === 'observation') {
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    // V2 格式的 observation
    const obsContent = json.content || json
    let displayContent = ''

    if (typeof obsContent === 'object' && obsContent !== null) {
      const obs = obsContent as Record<string, unknown>
      const parts: string[] = []
      if (obs.section) parts.push(`📑 ${obs.section}`)
      if (obs.facts_count) parts.push(`事实: ${obs.facts_count} 条`)
      if (obs.data_points_count)
        parts.push(`数据点: ${obs.data_points_count} 个`)
      if (obs.duplicates_removed)
        parts.push(`去重: ${obs.duplicates_removed} 条`)
      if (obs.insights && (obs.insights as string[]).length > 0) {
        parts.push(
          `洞察:\n${(obs.insights as string[]).map((i) => `  • ${i}`).join('\n')}`,
        )
      }
      if (obs.source_quality) parts.push(`来源质量: ${obs.source_quality}`)
      displayContent = parts.join('\n') || JSON.stringify(obsContent, null, 2)
    } else {
      displayContent =
        typeof json.result === 'string'
          ? json.result
          : JSON.stringify(json.result || obsContent)
    }

    const stepId = `obs_${Date.now()}_${target.reactSteps.length}`
    target.reactSteps.push({
      step: json.step || target.reactSteps.length + 1,
      type: 'observation',
      content: displayContent,
      tool: json.tool,
      queries: json.queries_executed,
      success: json.success !== false,
      timestamp: Date.now(),
      stepId, // 添加 stepId 用于关联详情
    })

    // 存储步骤详情用于右侧面板展示
    if (typeof obsContent === 'object' && obsContent !== null) {
      const obs = obsContent as Record<string, unknown>
      const stepDetail: StepDetailData = {
        stepId,
        type: (obs.agent as string) || 'observation',
        section: obs.section as string,
        searchResults: obs.search_results as StepDetailData['searchResults'],
        extractedFacts: obs.extracted_facts as StepDetailData['extractedFacts'],
        dataPoints: obs.data_points as StepDetailData['dataPoints'],
        insights: obs.insights as StepDetailData['insights'],
      }
      d.stepDetails.set(stepId, stepDetail)
      // 自动选中最新的步骤详情
      d.selectedStepDetail = stepDetail
    }
  } else if (json.type === 'section_draft') {
    // V2 章节撰写完成事件
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    const content = json.content || {}
    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'observation',
      content: `✍️ 章节「${content.section_title || '未知'}」撰写完成\n字数: ${content.word_count || 0}\n要点: ${(content.key_points || []).join('、')}`,
      timestamp: Date.now(),
    })
  } else if (json.type === 'section_content') {
    // V2 章节内容事件 - 用于"过程报告"tab的流式显示
    const content = json.content || {}
    const sectionContent = content.content || ''
    const sectionTitle = content.section_title || ''

    if (sectionContent) {
      // 使用 stepType 作为 key 查找或创建 detail
      const writingType = 'writing'

      // 如果没有找到写作步骤，创建一个（兜底逻辑）
      if (!d.researchSteps.find((s) => s.type === writingType)) {
        const newStep: ResearchStep = {
          id: writingType, // 使用 type 作为 id
          type: writingType,
          title: '✍️ 写作阶段',
          subtitle: '撰写研究报告',
          status: 'running',
        }
        d.researchSteps = [...d.researchSteps, newStep]
      }

      // 获取或创建详情 - 使用 stepType 作为 key
      let detail = d.researchDetails.get(writingType)
      if (!detail) {
        detail = {
          stepId: writingType,
          stepType: writingType,
          title: '写作阶段',
          streamingReport: '',
          searchResults: [],
          charts: [],
          sections: [], // 初始化 sections 数组
        }
        d.researchDetails.set(writingType, detail)
      }

      // 添加章节到 sections 数组
      const sectionId = content.section_id || `section_${Date.now()}`
      if (!detail.sections) {
        detail.sections = []
      }
      // 检查是否已存在，避免重复
      const existingIndex = detail.sections.findIndex((s) => s.id === sectionId)
      if (existingIndex >= 0) {
        detail.sections[existingIndex] = {
          id: sectionId,
          title: sectionTitle,
          content: sectionContent,
          wordCount: sectionContent.length,
        }
      } else {
        detail.sections.push({
          id: sectionId,
          title: sectionTitle,
          content: sectionContent,
          wordCount: sectionContent.length,
        })
      }

      // 累加章节内容到 streamingReport（保持向后兼容）
      const existingContent = detail.streamingReport || ''
      detail.streamingReport = existingContent
        ? `${existingContent}\n\n## ${sectionTitle}\n\n${sectionContent}`
        : `## ${sectionTitle}\n\n${sectionContent}`
      d.selectedResearchDetail = { ...detail }
      d.researchDataVersion += 1

      // 同时添加到 reactSteps
      if (!target.reactSteps) {
        target.reactSteps = []
      }
      target.reactSteps.push({
        step: target.reactSteps.length + 1,
        type: 'observation',
        content: `✍️ 章节「${sectionTitle}」已写入过程报告\n字数: ${sectionContent.length}\n要点: ${(content.key_points || []).slice(0, 2).join('、') || '无'}`,
        timestamp: Date.now(),
      })
    }
  } else if (json.type === 'report_draft') {
    // V2 报告草稿完成事件
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    const eventContent = json.content || ''
    const reportContent =
      typeof eventContent === 'string'
        ? eventContent
        : eventContent.content || ''
    const reportMeta: ReportDraftPayload =
      typeof eventContent === 'string' ? {} : eventContent

    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'observation',
      content: `📝 研究报告撰写完成\n字数: ${reportMeta.word_count || reportContent.length || 0}\n引用数: ${reportMeta.references_count || 0}`,
      timestamp: Date.now(),
    })

    // 存储报告内容到 streamingReport 用于"过程报告"tab显示 - 使用 stepType 作为 key
    if (reportContent) {
      const writingType = d.researchDetails.has('writing')
        ? 'writing'
        : 'generating'
      const detail = d.researchDetails.get(writingType)
      if (detail) {
        detail.streamingReport = reportContent
        d.selectedResearchDetail = { ...detail }
        d.researchDataVersion += 1
      }
      // 同时设置为聊天消息内容
      target.content = reportContent
    }

    // 标记写作步骤完成
    d.researchSteps = d.researchSteps.map((s) =>
      s.type === 'writing' || s.type === 'generating'
        ? { ...s, status: 'completed' as const }
        : s,
    )
  } else if (json.type === 'review') {
    // V2 审核反馈事件
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    const content = json.content || {}
    const score = content.quality_score || 0
    const passed = content.passed || content.verdict === 'pass' || score >= 7
    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'thought',
      content: `🔍 审核结果: 质量评分 ${score}/10\n${passed ? '✅ 审核通过' : '⚠️ 需要修订'}`,
      timestamp: Date.now(),
    })

    // 更新审核步骤状态
    d.researchSteps = d.researchSteps.map((s) =>
      s.type === 'reviewing'
        ? {
            ...s,
            status: passed ? ('completed' as const) : ('running' as const),
          }
        : s,
    )
  } else if (json.type === 'revision_complete') {
    // V2 修订完成事件
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    const content = json.content || {}
    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'observation',
      content: `📝 修订完成，共 ${content.changes_count || 0} 处修改`,
      timestamp: Date.now(),
    })
  } else if (json.type === 'error') {
    // V2 错误事件
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'thought',
      content: `❌ 错误: ${extractContent(json.content)}`,
      timestamp: Date.now(),
    })
  } else if (json.type === 'research_cancelled') {
    // 研究被取消事件
    if (!target.reactSteps) {
      target.reactSteps = []
    }
    target.reactSteps.push({
      step: target.reactSteps.length + 1,
      type: 'thought',
      content: `⏹️ 研究已被用户取消`,
      timestamp: Date.now(),
    })
    target.loading = false
    if (!target.content) {
      target.content = '⏹️ 研究已被用户取消'
    }
    // 标记所有研究步骤为完成
    d.researchSteps = d.researchSteps.map((s) => ({
      ...s,
      status: 'completed' as const,
    }))
  } else if (json.type === 'chart') {
    // 解包 content（后端将数据包在 content 里）
    const content = json.content || {}

    // 构建图表对象
    const chartObj = {
      id: uniqueId('chart_'),
      type: (content.chart_type || content.type || 'generated') as NonNullable<
        API.ChatItem['charts']
      >[number]['type'],
      title: content.title || '数据图表',
      echarts_option: content.echarts_option,
      image_base64: content.image || content.image_base64,
      data: content.data,
    }

    // 存入 target.charts（供报告使用）
    if (!target.charts) {
      target.charts = []
    }
    target.charts.push(chartObj)

    // 同时存入 research detail（供可视化面板使用）- 使用 stepType 作为 key
    const detail = d.researchDetails.get('analyzing')
    if (detail) {
      const detailChart = chartObj as unknown as ChartConfig
      if (!detail.charts) {
        detail.charts = []
      }
      detail.charts.push(detailChart)
      d.researchSteps = d.researchSteps.map((s) =>
        s.type === 'analyzing'
          ? {
              ...s,
              stats: { ...s.stats, chartsCount: detail.charts?.length || 0 },
            }
          : s,
      )
      d.selectedResearchDetail = { ...detail }
      d.researchDataVersion += 1
    } else {
      console.warn(
        `[前端] ⚠️ 未找到 analyzing detail，图表可能无法显示在可视化面板`,
      )
    }
  } else if (json.type === 'stock_quote') {
    // 股票实时行情
    const content = json.content || {}
    target.stockQuote = {
      code: content.code as string,
      name: content.name as string,
      price: content.price as string | number,
      change: content.change as string | number,
      change_percent: content.change_percent as string,
      high: content.high,
      low: content.low,
      volume: content.volume,
      turnover: content.turnover,
      open: content.open,
      prev_close: content.prev_close,
    }
  } else if (json.type === 'data_insight') {
    if (!target.insights) {
      target.insights = []
    }
    target.insights.push(...(json.insights || []))
  } else if (
    json.type === 'status' ||
    json.type === 'search_results' ||
    json.type === 'thinking_step'
  ) {
    // 兼容原有状态事件
    if (!target.thinks) {
      target.thinks = []
    }

    const lastThink = target.thinks[target.thinks.length - 1]
    const thinkType = json.type
    const thinkContent =
      json.subquery || (typeof json.content === 'string' ? json.content : '')

    if (lastThink?.type === thinkType) {
      lastThink.results!.push({
        id: uniqueId('think_result'),
        content: thinkContent,
        count: json.count,
      })
    } else {
      target.thinks.push({
        id: uniqueId('think_result'),
        type: thinkType as 'status' | 'search_results',
        results: [
          {
            id: uniqueId('think_result'),
            content: thinkContent,
            count: json.count,
          },
        ],
      })
    }
  } else if (json.type === 'search_result_item') {
    if (!target.search_results) {
      target.search_results = []
    }

    try {
      target.search_results.push({
        ...(json.result || {}),
        id: uniqueId('search-results'),
        host: json.result?.url ? new URL(json.result.url).host : '',
      } as NonNullable<API.ChatItem['search_results']>[number])
    } catch {
      // 忽略：单条 search_results 解析失败不中断整体流
    }
  } else if (json.type === 'thinking') {
    target.think = `${target.think || ''}${json.content || ''}`
  } else if (json.type === 'answer' || json.type === 'final_answer') {
    target.content = `${target.content}${json.content || ''}`
  } else if (json.type === 'reference_materials') {
    target.reference = json.content?.map((o) => ({
      id: o.reference_id as number,
      title: o.name as string,
      link: o.url as string,
      content: o.summary as string,
      source: o.source === 'local' ? 'knowledge' : 'web',
    }))
  }
}

/** 普通聊天流：无 `type` 的历史事件形态。 */
function reduceLegacyChatEvent(
  d: ResearchUiState,
  json: Extract<ResearchEvent, { type?: undefined }>,
): void {
  const target = d.chatItem

  if (json.content) {
    if (json.thinking) {
      target.think = `${target.think || ''}${json.content || ''}`
    } else {
      target.content = `${target.content || ''}${json.content || ''}`
    }
  }

  if (json.documents?.length) {
    // 历史形态：普通聊天流把文档直挂在 `documents`，前端按引用列表消费
    target.reference = json.documents as API.Reference[]
  }

  if (json.image_results) {
    target.image_results = json.image_results as API.ChatItem['image_results']
  }
}
