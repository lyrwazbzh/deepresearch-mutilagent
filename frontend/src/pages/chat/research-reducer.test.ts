/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { ChatRole, ChatType } from '@/configs'
import type { ResearchEvent } from '@/features/deep-research/types'
import { describe, expect, it, vi } from 'vitest'
import {
  createResearchUiState,
  reduceResearchEvent,
  type ResearchUiState,
} from './research-reducer'

/**
 * T66：归约器单测**不 mock 任何业务 module** —— 只喂事件、断言状态。
 * 这与改动前的形状证据形成对照：此前要驱动一条事件分支，集成测试得 mock 14 个 module。
 */

function chatItem(overrides: Partial<API.ChatItem> = {}): API.ChatItem {
  return {
    id: 1,
    role: ChatRole.Assistant,
    type: ChatType.Deepsearch,
    content: '',
    ...overrides,
  }
}

function createState(overrides: Partial<API.ChatItem> = {}): ResearchUiState {
  return createResearchUiState({ chatItem: chatItem(overrides) })
}

function reduce(
  state: ResearchUiState,
  ...events: ResearchEvent[]
): ResearchUiState {
  return events.reduce(reduceResearchEvent, state)
}

function stepOf(state: ResearchUiState, type: string) {
  return state.researchSteps.find((step) => step.type === type)
}

/** 先落一个 detail，供需要「已存在 detail」的分支使用。 */
function withDetail(type: string): ResearchUiState {
  return reduce(createState(), {
    type: 'research_step',
    content: { step_type: type, status: 'running', title: type },
  })
}

describe('reduceResearchEvent —— 深研事件到 UI 状态', () => {
  it('产出新状态且不写坏入参（纯映射）', () => {
    const first = createState()
    const second = reduce(first, {
      type: 'research_step',
      content: { step_type: 'researching', status: 'running' },
    })

    expect(second).not.toBe(first)
    expect(second.researchSteps).not.toBe(first.researchSteps)
    expect(first.researchSteps).toHaveLength(0)
    expect(first.chatItem.reactSteps).toBeUndefined()
    expect(second.researchSteps).toHaveLength(1)
  })

  it('outline_pending_approval：写入待审批、清错误、停掉 loading', () => {
    const next = reduce(createState({ loading: true }), {
      type: 'outline_pending_approval',
      session_id: 's1',
      outline_revision: 'r1',
      sections: [],
      research_questions: [],
    })

    expect(next.pendingOutline?.session_id).toBe('s1')
    expect(next.outlineApprovalError).toBeUndefined()
    expect(next.chatItem.loading).toBe(false)
  })

  it('research_start：开启 reactMode、推入计划步骤并重置研究态', () => {
    const seeded = reduce(withDetail('analyzing'), {
      type: 'research_complete',
      final_report: 'old report',
    })
    const next = reduce(seeded, { type: 'research_start', query: '电池产业' })

    expect(next.chatItem.reactMode).toBe(true)
    expect(next.chatItem.reactSteps?.[0]).toMatchObject({
      step: 0,
      type: 'plan',
      content: '🔬 开始深度研究: 电池产业',
    })
    expect(next.researchSteps).toHaveLength(0)
    expect(next.researchDetails.size).toBe(0)
    expect(next.selectedResearchDetail).toBeNull()
    expect(next.researchDataVersion).toBe(0)
    expect(next.pendingOutline).toBeNull()
  })

  it('research_step：新步骤以 step_type 为 id，并同步 snake_case → camelCase 统计', () => {
    const next = reduce(createState(), {
      type: 'research_step',
      content: {
        step_type: 'searching',
        status: 'running',
        title: '搜索',
        subtitle: 'sub',
        stats: { results_count: 3, sources_count: 2 },
      },
    })

    expect(next.researchSteps[0]).toMatchObject({
      id: 'searching',
      type: 'searching',
      title: '搜索',
      subtitle: 'sub',
      status: 'running',
    })
    expect(next.researchSteps[0].stats?.resultsCount).toBe(3)
    expect(next.researchSteps[0].stats?.sourcesCount).toBe(2)
    expect(next.researchDetails.get('searching')?.title).toBe('搜索')
    // searching 步骤会被自动选中
    expect(next.selectedResearchDetail?.stepType).toBe('searching')
  })

  it('research_step：同 step_type 再到达时更新而非追加', () => {
    const first = reduce(createState(), {
      type: 'research_step',
      content: { step_type: 'analyzing', status: 'running', title: '分析' },
    })
    const next = reduce(first, {
      type: 'research_step',
      content: {
        step_type: 'analyzing',
        status: 'completed',
        stats: { charts_count: 4 },
      },
    })

    expect(next.researchSteps).toHaveLength(1)
    expect(stepOf(next, 'analyzing')?.status).toBe('completed')
    expect(stepOf(next, 'analyzing')?.title).toBe('分析')
    expect(stepOf(next, 'analyzing')?.stats?.chartsCount).toBe(4)
  })

  it('search_results：整体替换结果并回写 resultsCount', () => {
    const next = reduce(withDetail('searching'), {
      type: 'search_results',
      content: {
        results: [
          { id: 'a', title: 'A', source: 'web', url: 'https://a.test' },
          { id: 'b', title: 'B', source: 'web' },
        ],
      },
    })

    const detail = next.researchDetails.get('searching')
    expect(detail?.searchResults).toHaveLength(2)
    expect(detail?.searchResults?.[0].title).toBe('A')
    expect(stepOf(next, 'searching')?.stats?.resultsCount).toBe(2)
    expect(next.selectedResearchDetail?.searchResults).toHaveLength(2)
    expect(next.researchDataVersion).toBe(1)
  })

  it('search_results：isIncremental 时累加', () => {
    const first = reduce(withDetail('searching'), {
      type: 'search_results',
      content: { results: [{ id: 'a', title: 'A', source: 'web' }] },
    })
    const next = reduce(first, {
      type: 'search_results',
      content: {
        results: [{ id: 'b', title: 'B', source: 'web' }],
        isIncremental: true,
      },
    })

    expect(next.researchDetails.get('searching')?.searchResults).toHaveLength(2)
  })

  it('knowledge_graph：写入图谱并选中', () => {
    const next = reduce(withDetail('analyzing'), {
      type: 'knowledge_graph',
      content: {
        graph: {
          nodes: [{ id: 'n1' }],
          edges: [{ source: 'n1', target: 'n1' }],
        },
        stats: { entities_count: 1, relations_count: 1 },
      },
    })

    expect(
      next.researchDetails.get('analyzing')?.knowledgeGraph?.nodes,
    ).toHaveLength(1)
    expect(next.selectedResearchDetail?.knowledgeGraph?.edges).toHaveLength(1)
    expect(next.researchDataVersion).toBe(1)
  })

  it('knowledge_graph：无 detail 时告警但不中断', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const next = reduce(createState(), {
      type: 'knowledge_graph',
      content: { graph: { nodes: [{ id: 'n1' }], edges: [] } },
    })
    warn.mockRestore()

    expect(next.researchDetails.size).toBe(0)
    expect(next.selectedResearchDetail).toBeNull()
  })

  it('charts：写入 detail 与 chatItem.charts，并回写 chartsCount', () => {
    const next = reduce(withDetail('analyzing'), {
      type: 'charts',
      content: { charts: [{ chart_type: 'bar', title: '销量' }] },
    })

    expect(next.researchDetails.get('analyzing')?.charts).toHaveLength(1)
    expect(next.chatItem.charts).toHaveLength(1)
    expect(stepOf(next, 'analyzing')?.stats?.chartsCount).toBe(1)
  })

  it('phase：写入思考步骤，writing 阶段建步骤与详情并自动选中', () => {
    const next = reduce(createState(), {
      type: 'phase',
      phase: 'writing',
      content: { agent: 'writer', content: '开始写作' },
    })

    expect(next.chatItem.reactSteps?.[0]).toMatchObject({
      step: 1,
      type: 'thought',
      content: '✍️ 写作阶段: 开始写作',
    })
    expect(stepOf(next, 'writing')?.status).toBe('running')
    expect(next.researchDetails.get('writing')?.streamingReport).toBe('')
    expect(next.selectedResearchDetail?.stepType).toBe('writing')
  })

  it('phase：未知阶段只写思考步骤', () => {
    const next = reduce(createState(), {
      type: 'phase',
      phase: 'mystery',
      content: 'x',
    })

    expect(next.chatItem.reactSteps).toHaveLength(1)
    expect(next.researchSteps).toHaveLength(0)
  })

  it('outline：把章节与问题渲染成计划步骤', () => {
    const next = reduce(createState(), {
      type: 'outline',
      content: {
        outline: [{ title: '第一章', description: '描述' }],
        research_questions: ['问题一'],
      },
    })

    const content = next.chatItem.reactSteps?.[0].content ?? ''
    expect(content).toContain('**研究大纲**')
    expect(content).toContain('1. **第一章**')
    expect(content).toContain('• 问题一')
  })

  it('research_complete：写报告、引用、完成所有步骤', () => {
    const seeded = withDetail('writing')
    const next = reduce(seeded, {
      type: 'research_complete',
      final_report: '最终报告',
      references: [{ title: '来源A', url: 'https://a.test' }],
    })

    expect(next.chatItem.content).toBe('最终报告')
    expect(next.researchDetails.get('writing')?.streamingReport).toBe(
      '最终报告',
    )
    expect(next.chatItem.reference).toEqual([
      {
        id: 1,
        title: '来源A',
        link: 'https://a.test',
        content: '',
        source: 'web',
      },
    ])
    expect(
      next.researchSteps.every((step) => step.status === 'completed'),
    ).toBe(true)
    expect(next.pendingOutline).toBeNull()
    // 该分支刻意自增两次：一次为 detail 更新、一次为「确保触发重新计算」
    expect(next.researchDataVersion).toBe(2)
  })

  it('mode：react / optimized 都会开启 reactMode', () => {
    const next = reduce(
      createState(),
      { type: 'research_resumed', mode: 'react' },
      { type: 'research_resumed', mode: 'optimized' },
    )

    expect(next.chatItem.reactMode).toBe(true)
  })

  it('react_start 也会开启 reactMode（仅凭 type，不带 mode 字段）', () => {
    const next = reduce(createState(), { type: 'react_start' })

    expect(next.chatItem.reactMode).toBe(true)
  })

  it('plan：写入研究计划并推入计划步骤', () => {
    const next = reduce(createState(), {
      type: 'plan',
      understanding: '理解',
      strategy: '策略',
      sub_queries: [{ query: 'q', purpose: 'p', tool: 't' }],
      expected_aspects: ['a'],
    })

    expect(next.chatItem.researchPlan).toEqual({
      understanding: '理解',
      strategy: '策略',
      subQueries: [{ query: 'q', purpose: 'p', tool: 't' }],
      expectedAspects: ['a'],
    })
    expect(next.chatItem.reactSteps?.[0].content).toContain('**研究计划**')
  })

  it('plan：无 understanding 时不处理', () => {
    const next = reduce(createState(), { type: 'plan', strategy: '策略' })

    expect(next.chatItem.researchPlan).toBeUndefined()
    expect(next.chatItem.reactSteps).toBeUndefined()
  })

  it('thought：解包 V2 content', () => {
    const next = reduce(createState(), {
      type: 'thought',
      content: { agent: 'scout', content: '思考内容' },
    })

    expect(next.chatItem.reactSteps?.[0]).toMatchObject({
      step: 1,
      type: 'thought',
      content: '思考内容',
    })
  })

  it('action：并行搜索展开查询列表', () => {
    const next = reduce(createState(), {
      type: 'action',
      content: {
        tool: 'parallel_search',
        queries: ['q1', 'q2'],
        section: '电池',
      },
    })

    expect(next.chatItem.reactSteps?.[0].content).toBe(
      '并行搜索 (电池) 2 个查询:\n• q1\n• q2',
    )
    expect(next.chatItem.reactSteps?.[0].queries).toEqual(['q1', 'q2'])
  })

  it('observation：生成详情并选中，success 默认 true', () => {
    const next = reduce(createState(), {
      type: 'observation',
      content: {
        agent: 'scout',
        section: '电池',
        facts_count: 2,
        insights: ['洞察'],
      },
      tool: 'search',
    })

    const pushed = next.chatItem.reactSteps?.[0]
    expect(pushed?.type).toBe('observation')
    expect(pushed?.content).toContain('📑 电池')
    expect(pushed?.success).toBe(true)
    expect(next.selectedStepDetail?.type).toBe('scout')
    expect(next.selectedStepDetail?.insights).toEqual(['洞察'])
    expect(next.stepDetails.size).toBe(1)
  })

  it('section_draft：渲染章节完成信息', () => {
    const next = reduce(createState(), {
      type: 'section_draft',
      content: { section_title: '第一章', word_count: 12, key_points: ['k1'] },
    })

    expect(next.chatItem.reactSteps?.[0].content).toContain(
      '章节「第一章」撰写完成',
    )
    expect(next.chatItem.reactSteps?.[0].content).toContain('字数: 12')
  })

  it('section_content：兜底建写作步骤、累积章节与过程报告', () => {
    const first = reduce(createState(), {
      type: 'section_content',
      content: { section_id: 's1', section_title: '第一章', content: '正文一' },
    })
    const next = reduce(first, {
      type: 'section_content',
      content: {
        section_id: 's1',
        section_title: '第一章',
        content: '正文一改',
      },
    })

    const detail = next.researchDetails.get('writing')
    expect(stepOf(next, 'writing')?.status).toBe('running')
    // 同 section_id 覆盖不重复
    expect(detail?.sections).toHaveLength(1)
    expect(detail?.sections?.[0].content).toBe('正文一改')
    expect(detail?.streamingReport).toContain('## 第一章')
    expect(next.chatItem.reactSteps).toHaveLength(2)
    expect(next.researchDataVersion).toBe(2)
  })

  it('report_draft：写报告正文、完成写作步骤', () => {
    const next = reduce(withDetail('writing'), {
      type: 'report_draft',
      content: { content: '报告草稿', word_count: 4, references_count: 2 },
    })

    expect(next.researchDetails.get('writing')?.streamingReport).toBe(
      '报告草稿',
    )
    expect(next.chatItem.content).toBe('报告草稿')
    expect(stepOf(next, 'writing')?.status).toBe('completed')
    expect(next.chatItem.reactSteps?.[0].content).toContain('字数: 4')
  })

  it('report_draft：content 为纯字符串时也能处理', () => {
    const next = reduce(withDetail('writing'), {
      type: 'report_draft',
      content: '直接给正文',
    })

    expect(next.chatItem.content).toBe('直接给正文')
    expect(next.chatItem.reactSteps?.[0].content).toContain('引用数: 0')
  })

  it('review：通过时完成审核步骤，未通过时保持 running', () => {
    const passed = reduce(withDetail('reviewing'), {
      type: 'review',
      content: { quality_score: 8 },
    })
    const failed = reduce(withDetail('reviewing'), {
      type: 'review',
      content: { quality_score: 3, verdict: 'revise' },
    })

    expect(stepOf(passed, 'reviewing')?.status).toBe('completed')
    expect(passed.chatItem.reactSteps?.[0].content).toContain('✅ 审核通过')
    expect(stepOf(failed, 'reviewing')?.status).toBe('running')
    expect(failed.chatItem.reactSteps?.[0].content).toContain('⚠️ 需要修订')
  })

  it('revision_complete：渲染修订条数', () => {
    const next = reduce(createState(), {
      type: 'revision_complete',
      content: { changes_count: 4 },
    })

    expect(next.chatItem.reactSteps?.[0].content).toContain('共 4 处修改')
  })

  it('error：把错误写进思考步骤', () => {
    const next = reduce(createState(), { type: 'error', content: 'boom' })

    expect(next.chatItem.reactSteps?.[0].content).toBe('❌ 错误: boom')
  })

  it('research_cancelled：停 loading、兜底内容、完成所有步骤', () => {
    const next = reduce(withDetail('researching'), {
      type: 'research_cancelled',
    })

    expect(next.chatItem.loading).toBe(false)
    expect(next.chatItem.content).toBe('⏹️ 研究已被用户取消')
    expect(
      next.researchSteps.every((step) => step.status === 'completed'),
    ).toBe(true)
  })

  it('chart：同时进 chatItem.charts 与 analyzing 详情', () => {
    const next = reduce(withDetail('analyzing'), {
      type: 'chart',
      content: { chart_type: 'line', title: '趋势', echarts_option: { x: 1 } },
    })

    expect(next.chatItem.charts).toHaveLength(1)
    expect(next.chatItem.charts?.[0]).toMatchObject({
      type: 'line',
      title: '趋势',
      echarts_option: { x: 1 },
    })
    expect(next.researchDetails.get('analyzing')?.charts).toHaveLength(1)
    expect(stepOf(next, 'analyzing')?.stats?.chartsCount).toBe(1)
  })

  it('stock_quote：写入行情', () => {
    const next = reduce(createState(), {
      type: 'stock_quote',
      content: {
        code: '000001',
        name: '平安银行',
        price: 10,
        change_percent: '+1%',
      },
    })

    expect(next.chatItem.stockQuote).toMatchObject({
      code: '000001',
      name: '平安银行',
      price: 10,
      change_percent: '+1%',
    })
  })

  it('data_insight：累加洞察', () => {
    const next = reduce(
      createState(),
      { type: 'data_insight', insights: ['a'] },
      { type: 'data_insight', insights: ['b'] },
    )

    expect(next.chatItem.insights).toEqual(['a', 'b'])
  })

  it('status / thinking_step：同类合并、异类新开一条', () => {
    const next = reduce(
      createState(),
      { type: 'status', subquery: 'q1', count: 1 },
      { type: 'status', subquery: 'q2', count: 2 },
      { type: 'thinking_step', subquery: 'q3' },
    )

    expect(next.chatItem.thinks).toHaveLength(2)
    expect(next.chatItem.thinks?.[0].results).toHaveLength(2)
    expect(next.chatItem.thinks?.[0].results?.[1].content).toBe('q2')
    expect(next.chatItem.thinks?.[1].type).toBe('thinking_step')
  })

  it('search_result_item：补 id 与 host', () => {
    const next = reduce(createState(), {
      type: 'search_result_item',
      result: {
        subquery: 'q',
        url: 'https://example.test/a',
        name: 'n',
        summary: 's',
        snippet: 'sn',
        siteName: 'site',
        siteIcon: 'icon',
      },
    })

    expect(next.chatItem.search_results?.[0].host).toBe('example.test')
    expect(next.chatItem.search_results?.[0].id).toBeTruthy()
  })

  it('search_result_item：坏 URL 不抛出（只留下空列表）', () => {
    const next = reduce(createState(), {
      type: 'search_result_item',
      result: { url: 'not a url', name: 'n' },
    })

    expect(next.chatItem.search_results).toEqual([])
  })

  it('thinking：追加思考文本', () => {
    const next = reduce(
      createState(),
      { type: 'thinking', content: 'a' },
      { type: 'thinking', content: 'b' },
    )

    expect(next.chatItem.think).toBe('ab')
  })

  it('answer / final_answer：追加正文', () => {
    const next = reduce(
      createState({ content: '前' }),
      { type: 'answer', content: '中' },
      { type: 'final_answer', content: '后' },
    )

    expect(next.chatItem.content).toBe('前中后')
  })

  it('reference_materials：映射引用列表', () => {
    const next = reduce(createState(), {
      type: 'reference_materials',
      content: [
        {
          reference_id: 1,
          name: '本地',
          url: 'k://1',
          summary: 's',
          source: 'local',
        },
      ],
    })

    expect(next.chatItem.reference).toEqual([
      {
        id: 1,
        title: '本地',
        link: 'k://1',
        content: 's',
        source: 'knowledge',
      },
    ])
  })
})

describe('reduceResearchEvent —— 普通聊天流（无 type 的历史形态）', () => {
  function legacyState() {
    return createResearchUiState({
      chatItem: chatItem({ type: ChatType.Normal }),
    })
  }

  it('content + thinking：写入思考而非正文', () => {
    const next = reduce(legacyState(), { content: '想', thinking: true })

    expect(next.chatItem.think).toBe('想')
    expect(next.chatItem.content).toBe('')
  })

  it('content：追加到正文', () => {
    const next = reduce(legacyState(), { content: 'a' }, { content: 'b' })

    expect(next.chatItem.content).toBe('ab')
  })

  it('documents / image_results：挂到引用与图片结果', () => {
    const next = reduce(legacyState(), {
      content: 'x',
      documents: [{ title: 'D', link: 'https://d.test' }],
      image_results: { images: [] },
    })

    expect(next.chatItem.reference).toHaveLength(1)
    expect(next.chatItem.image_results).toEqual({ images: [] })
  })
})
