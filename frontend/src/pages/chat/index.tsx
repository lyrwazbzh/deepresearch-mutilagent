/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import * as api from '@/api'
import type { Message, ResearchTimeline } from '@/api/session'
import ComPageLayout from '@/components/page-layout'
import ComSender, { AttachmentInfo } from '@/components/sender'
import { ChatRole, ChatType } from '@/configs'
import { OutlineApprovalPanel } from '@/features/deep-research/OutlineApprovalPanel'
import { consumeResearchStream } from '@/features/deep-research/research-stream'
import type {
  EditableResearchPlan,
  OutlinePendingApprovalEvent,
  ResearchEvent,
} from '@/features/deep-research/types'
import { deviceActions, deviceState } from '@/store/device'
import { sessionState } from '@/store/session'
import { usePageTransport } from '@/utils'
import { useUnmount } from 'ahooks'
import { uniqueId } from 'lodash-es'
import { message } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { proxy, useSnapshot } from 'valtio'
import ChatMessage from './component/chat-message'
import Drawer from './component/drawer'
import Source from './component/source'
import StepDetailPanel, { StepDetailData } from './component/step-detail-panel'
import ResearchDetail, { ResearchDetailData, ResearchStep } from './component/research-detail'
import styles from './index.module.scss'
import {
  createResearchUiState,
  reduceResearchEvent,
  type ResearchUiState,
} from './research-reducer'
import { createChatId, createChatIdText, transportToChatEnter } from './shared'

async function scrollToBottom() {
  await new Promise((resolve) => setTimeout(resolve))

  const threshold = 200
  const distanceToBottom =
    document.documentElement.scrollHeight -
    document.documentElement.scrollTop -
    document.documentElement.clientHeight

  if (distanceToBottom <= threshold) {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: 'smooth',
    })
  }
}

async function getApprovalErrorMessage(error: unknown): Promise<string> {
  const requestError = error as {
    message?: string
    response?: { data?: unknown }
  }
  const data = requestError.response?.data

  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === 'string' && detail) return detail
  }

  if (data instanceof ReadableStream) {
    try {
      const raw = await new Response(data).text()
      const parsed = JSON.parse(raw) as { detail?: unknown }
      if (typeof parsed.detail === 'string' && parsed.detail) {
        return parsed.detail
      }
    } catch {
      // Fall through to the transport error message.
    }
  }

  return requestError.message || 'Unable to approve the outline'
}

export default function Index() {
  const { id } = useParams()
  const { data: ctx } = usePageTransport(transportToChatEnter)

  const [currentChatItem, setCurrentChatItem] = useState<API.ChatItem | null>(
    null,
  )

  // 步骤详情状态 (旧版)
  const [selectedStepDetail, setSelectedStepDetail] = useState<StepDetailData | null>(null)
  const stepDetailsRef = useRef<Map<string, StepDetailData>>(new Map())

  // 研究过程状态 (新版)
  const [researchSteps, setResearchSteps] = useState<ResearchStep[]>([])
  const researchStepsRef = useRef<ResearchStep[]>([])  // 保持最新引用，供事件处理器使用
  const [selectedResearchDetail, setSelectedResearchDetail] = useState<ResearchDetailData | null>(null)
  const researchDetailsRef = useRef<Map<string, ResearchDetailData>>(new Map())
  // 版本计数器 - 用于触发 aggregatedResearchData 重新计算
  const [researchDataVersion, setResearchDataVersion] = useState(0)
  const [diagnostics, setDiagnostics] = useState<ResearchTimeline | null>(null)
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false)
  const [diagnosticsError, setDiagnosticsError] = useState<string>()
  const [pendingOutline, setPendingOutline] =
    useState<OutlinePendingApprovalEvent | null>(null)
  const [approvingOutline, setApprovingOutline] = useState(false)
  const [outlineApprovalError, setOutlineApprovalError] = useState<string>()
  const approvalInFlightRef = useRef(false)
  const streamAbortRef = useRef<AbortController | null>(null)
  const researchStreamConsumerRef = useRef<
    ((stream: ReadableStream<Uint8Array>) => Promise<void>) | null
  >(null)

  // 同步 researchSteps 到 ref
  useEffect(() => {
    researchStepsRef.current = researchSteps
  }, [researchSteps])

  // 附件状态管理
  const [attachments, setAttachments] = useState<AttachmentInfo[]>([])
  const attachmentPollingRef = useRef<NodeJS.Timeout | null>(null)

  const [chat] = useState(() => {
    return proxy({
      list: [] as API.ChatItem[],
    })
  })
  const { list } = useSnapshot(chat) as {
    list: API.ChatItem[]
  }

  const loading = useMemo(() => {
    return list.some((o) => o.loading)
  }, [list])
  const loadingRef = useRef(loading)
  loadingRef.current = loading

  const loadDiagnostics = useCallback(async () => {
    if (!id) return
    setDiagnosticsLoading(true)
    setDiagnosticsError(undefined)
    try {
      const response = await api.session.getResearchTimeline(id)
      setDiagnostics(response.data)
    } catch (error) {
      const requestError = error as { response?: { status?: number } }
      if (requestError.response?.status === 404) {
        setDiagnostics({ runs: [], events: [], next_cursor: null })
      } else {
        setDiagnosticsError('诊断记录加载失败，请检查后端服务')
      }
    } finally {
      setDiagnosticsLoading(false)
    }
  }, [id])

  useEffect(() => {
    setDiagnostics(null)
    setDiagnosticsError(undefined)
  }, [id])

  useEffect(() => {
    if (!loading) void loadDiagnostics()
  }, [loadDiagnostics, loading])
  useEffect(() => {
    deviceActions.setChatting(loading)
  }, [loading])
  useUnmount(() => {
    deviceActions.setChatting(false)
    // 清理轮询
    if (attachmentPollingRef.current) {
      clearInterval(attachmentPollingRef.current)
    }
  })

  const currentSessionIdRef = useRef<string | null>(null)
  const activeRouteSessionIdRef = useRef<string | undefined>(id)

  useEffect(() => {
    if (activeRouteSessionIdRef.current !== id) {
      streamAbortRef.current?.abort()
      streamAbortRef.current = null
    }
    activeRouteSessionIdRef.current = id
  }, [id])

  // 停止生成
  const handleStop = useCallback(async () => {
    streamAbortRef.current?.abort()
    streamAbortRef.current = null

    // 调用后端取消 API
    if (currentSessionIdRef.current) {
      try {
        await api.session.cancelResearch(currentSessionIdRef.current)
      } catch (e) {
        console.error('[handleStop] 调用取消 API 失败:', e)
      }
    }

    // 停止当前聊天项的加载状态
    const loadingItem = chat.list.find(item => item.loading)
    if (loadingItem) {
      loadingItem.loading = false
      if (!loadingItem.content) {
        loadingItem.content = '⏹️ 已停止生成'
      }
    }

    // 更新研究步骤状态
    setResearchSteps(prev => prev.map(s =>
      s.status === 'running' ? { ...s, status: 'completed' as const } : s
    ))
  }, [chat])

  // 轮询检查附件处理状态
  useEffect(() => {
    const pendingAttachments = attachments.filter(
      att => att.status === 'pending' || att.status === 'processing'
    )

    if (pendingAttachments.length > 0 && !attachmentPollingRef.current) {
      attachmentPollingRef.current = setInterval(async () => {
        for (const att of pendingAttachments) {
          try {
            const res = await api.session.getAttachment(att.id)
            if (res.data) {
              setAttachments(prev =>
                prev.map(a =>
                  a.id === att.id ? { ...a, status: res.data.status } : a
                )
              )
            }
          } catch (e) {
            console.error('Failed to check attachment status', e)
          }
        }
      }, 2000)
    } else if (pendingAttachments.length === 0 && attachmentPollingRef.current) {
      clearInterval(attachmentPollingRef.current)
      attachmentPollingRef.current = null
    }

    return () => {
      if (attachmentPollingRef.current) {
        clearInterval(attachmentPollingRef.current)
        attachmentPollingRef.current = null
      }
    }
  }, [attachments])

  // 上传附件
  const handleUploadAttachment = useCallback(async (file: File) => {
    if (!id) {
      message.error('请先创建会话')
      return null
    }

    // 添加临时附件
    const tempId = uniqueId('temp-attachment-')
    setAttachments(prev => [
      ...prev,
      { id: tempId, filename: file.name, status: 'uploading' }
    ])

    try {
      const res = await api.session.uploadAttachment(id, file)
      if (res.data) {
        // 替换临时附件为真实附件
        setAttachments(prev =>
          prev.map(a =>
            a.id === tempId
              ? { id: res.data.id, filename: res.data.filename, status: res.data.status }
              : a
          )
        )
        message.success(`附件 ${file.name} 上传成功`)
        return res.data
      }
    } catch (e) {
      message.error(`附件上传失败: ${(e as Error).message || '未知错误'}`)
      // 移除失败的附件
      setAttachments(prev => prev.filter(a => a.id !== tempId))
    }
    return null
  }, [id])

  // 移除附件
  const handleRemoveAttachment = useCallback(async (attachmentId: string) => {
    try {
      // 只有非临时 ID 才需要调用删除 API
      if (!attachmentId.startsWith('temp-')) {
        await api.session.deleteAttachment(attachmentId)
      }
      setAttachments(prev => prev.filter(a => a.id !== attachmentId))
    } catch (e) {
      console.error('Failed to delete attachment', e)
    }
  }, [])

  const sendChat = useCallback(
    async (
      target: API.ChatItem,
      chatMessage: string,
      attachmentIds?: string[],
      streamOverride?: ReadableStream<Uint8Array>,
      expectedSessionId?: string,
    ) => {
      const streamSessionId = expectedSessionId || id
      if (activeRouteSessionIdRef.current !== streamSessionId) return
      setCurrentChatItem(target)
      target.loading = true
      const controller = new AbortController()
      streamAbortRef.current = controller

      // T66：线事件 → UI 状态由纯归约器完成（research-reducer.ts）——
      // 页面不再内联 30 个事件分支，只负责把归约结果写回 ref 与 state。
      let researchUiState = createResearchUiState({
        chatItem: target,
        researchSteps: researchStepsRef.current,
        researchDetails: researchDetailsRef.current,
        stepDetails: stepDetailsRef.current,
      })

      const applyResearchState = (next: ResearchUiState) => {
        researchUiState = next
        Object.assign(target, next.chatItem)
        researchStepsRef.current = next.researchSteps
        researchDetailsRef.current = next.researchDetails
        stepDetailsRef.current = next.stepDetails
        setResearchSteps(next.researchSteps)
        setSelectedResearchDetail(next.selectedResearchDetail)
        setSelectedStepDetail(next.selectedStepDetail)
        setPendingOutline(next.pendingOutline)
        setOutlineApprovalError(next.outlineApprovalError)
        setResearchDataVersion(next.researchDataVersion)
      }

      try {
        let res
        if (streamOverride) {
          res = { data: streamOverride }
        } else if (target.type === ChatType.Deepsearch) {
          res = await api.session.deepsearch({
            query: chatMessage,
            session_id: id,  // 传递会话 ID 用于检查点保存
            search_modes: deviceState.searchModes as string[],  // 传递搜索模式
            // P-15：本地知识库模式必须携带 kb_name —— 后端 `Scout._execute_local_search`
            // 在 kb_name 为空时**静默跳过**本地检索，用户只会拿到零结果且无任何报错
            kb_name: (deviceState.searchModes as string[]).includes('local')
              ? (deviceState.kbName || undefined)
              : undefined,
          }, { signal: controller.signal })
        } else if (attachmentIds && attachmentIds.length > 0) {
          // 使用带附件的聊天接口
          res = await api.session.chatWithAttachments({
            session_id: id!,
            question: chatMessage,
            attachment_ids: attachmentIds,
          }, { signal: controller.signal })
        } else {
          res = await api.session.chat({
            session_id: id!,
            question: chatMessage,
          }, { signal: controller.signal })
        }

        if (activeRouteSessionIdRef.current !== streamSessionId) return

        const consumeTargetStream = async (
          stream: ReadableStream<Uint8Array>,
        ) => {
          await consumeResearchStream(stream, {
            onEvent: (event) => {
              if (activeRouteSessionIdRef.current !== streamSessionId) return
              parseData(event)
              void scrollToBottom()
            },
            onProtocolError: (error, raw) => {
              if (activeRouteSessionIdRef.current !== streamSessionId) return
              console.error('Research stream protocol error', error, raw)
            },
            onConnectionError: (error) => {
              if (
                activeRouteSessionIdRef.current === streamSessionId &&
                !controller.signal.aborted
              ) {
                console.error('Research stream connection error', error)
                message.error(
                  'Research stream disconnected. You can resume it later.',
                )
              }
            },
          })
        }

        // 存储 reader 和 session ID 用于取消
        currentSessionIdRef.current = streamSessionId || null

        researchStreamConsumerRef.current = consumeTargetStream
        await consumeTargetStream(res.data as ReadableStream<Uint8Array>)

      } finally {
        if (streamAbortRef.current === controller) {
          streamAbortRef.current = null
        }
        target.loading = false
      }

      // 事件 → 状态的映射全部在纯归约器里（见 research-reducer.ts）。
      function parseData(event: ResearchEvent) {
        applyResearchState(reduceResearchEvent(researchUiState, event))
      }
    },
    [id],
  )

  const handleApproveOutline = useCallback(
    async (plan: EditableResearchPlan) => {
      if (!id || !pendingOutline || approvalInFlightRef.current) return

      approvalInFlightRef.current = true
      setApprovingOutline(true)
      setOutlineApprovalError(undefined)
      const controller = new AbortController()
      streamAbortRef.current = controller

      try {
        const response = await api.session.approveResearchOutline(
          id,
          {
            outline_revision: pendingOutline.outline_revision,
            sections: plan.sections,
            research_questions: plan.researchQuestions,
          },
          { signal: controller.signal, errorToast: false },
        )
        const consume = researchStreamConsumerRef.current
        if (!consume) {
          throw new Error('Research stream handler is unavailable')
        }

        setPendingOutline(null)
        await consume(response.data as ReadableStream<Uint8Array>)
      } catch (error) {
        setOutlineApprovalError(await getApprovalErrorMessage(error))
        throw error
      } finally {
        approvalInFlightRef.current = false
        setApprovingOutline(false)
        if (streamAbortRef.current === controller) {
          streamAbortRef.current = null
        }
      }
    },
    [id, pendingOutline],
  )

  const send = useCallback(
    async (message: string, attachmentIds?: string[]) => {
      if (loadingRef.current) return
      if (!message && (!attachmentIds || attachmentIds.length === 0)) return

      chat.list.push({
        id: createChatId(),
        role: ChatRole.User,
        type: ChatType.Normal,
        content: message || '(附件问答)',
      })

      chat.list.push({
        id: createChatId(),
        role: ChatRole.Assistant,
        type: (deviceState.searchModes as string[]).length > 0 ? ChatType.Deepsearch : ChatType.Normal,
        content: '',
      })
      scrollToBottom()

      // 保存用户消息到数据库
      if (id) {
        try {
          await api.session.addMessage(id, {
            role: 'user',
            content: message || '(附件问答)',
          })
        } catch (e) {
          console.error('Failed to save user message:', e)
        }
      }

      const target = chat.list[chat.list.length - 1]

      await sendChat(target, message || '请分析附件内容', attachmentIds)

      // 保存助手回复到数据库
      if (id && target.content) {
        try {
          await api.session.addMessage(id, {
            role: 'assistant',
            content: target.content,
            thinking: target.think,
            references_data: target.reference ? { references: target.reference } : undefined,
          })
        } catch (e) {
          console.error('Failed to save assistant message:', e)
        }
      }

      // 发送后清空附件列表
      if (attachmentIds && attachmentIds.length > 0) {
        setAttachments([])
      }
    },
    [chat, sendChat, id],
  )
  const hasSentInitialMessage = useRef(false)
  const hasLoadedCheckpoint = useRef(false)
  const hasLoadedMessages = useRef(false)
  const previousIdRef = useRef<string | undefined>(undefined)
  const messagesLoadBarrierRef = useRef<{
    sessionId: string | undefined
    promise: Promise<void>
    resolve: () => void
  }>({
    sessionId: undefined,
    promise: Promise.resolve(),
    resolve: () => undefined,
  })

  // 当 session ID 变化时，重置加载状态
  useEffect(() => {
    if (id !== previousIdRef.current) {
      previousIdRef.current = id
      hasLoadedMessages.current = false
      hasLoadedCheckpoint.current = false
      hasSentInitialMessage.current = false
      let resolveMessagesLoaded: (value?: void) => void = () => undefined
      const messagesLoadedPromise = new Promise<void>((resolve) => {
        resolveMessagesLoaded = resolve
      })
      messagesLoadBarrierRef.current = {
        sessionId: id,
        promise: messagesLoadedPromise,
        resolve: resolveMessagesLoaded,
      }
      // 清空消息列表和研究状态
      chat.list.length = 0
      setResearchSteps([])
      researchStepsRef.current = []
      researchDetailsRef.current.clear()
      setSelectedResearchDetail(null)
      setResearchDataVersion(0)
      setCurrentChatItem(null)
      setPendingOutline(null)
      setOutlineApprovalError(undefined)
    }
  }, [id, chat])

  // 加载会话历史消息
  useEffect(() => {
    if (!id || hasLoadedMessages.current) return
    const loadId = id
    const loadBarrier = messagesLoadBarrierRef.current

    // 辅助函数：将消息数组填充到 chat.list
    function populateMessages(messages: Message[]) {
      const liveItems = [...chat.list]
      const restoredItems: API.ChatItem[] = []
      for (const msg of messages) {
        const chatItem: API.ChatItem = {
          id: createChatId(),
          role: msg.role === 'user' ? ChatRole.User : ChatRole.Assistant,
          type: msg.role === 'assistant' && msg.content?.length > 1000 ? ChatType.Deepsearch : ChatType.Normal,
          content: msg.content || '',
        }

        // 恢复助手消息的额外数据
        if (msg.role === 'assistant') {
          if (msg.thinking) {
            chatItem.think = msg.thinking
          }
          if (msg.references_data?.references) {
            chatItem.reference = msg.references_data.references as API.Reference[]
          }
        }

        restoredItems.push(chatItem)
      }

      for (const liveItem of liveItems) {
        let matchingIndex = -1
        for (let index = restoredItems.length - 1; index >= 0; index -= 1) {
          const restoredItem = restoredItems[index]
          if (
            restoredItem.role === liveItem.role &&
            restoredItem.content === liveItem.content
          ) {
            matchingIndex = index
            break
          }
        }

        if (matchingIndex >= 0) {
          restoredItems[matchingIndex] = liveItem
        } else {
          restoredItems.push(liveItem)
        }
      }

      chat.list.length = 0
      chat.list.push(...restoredItems)
    }

    // 优先使用 store 中预加载的数据
    const cachedSession = sessionState.currentSession
    if (cachedSession && cachedSession.id === id && cachedSession.messages?.length > 0) {
      hasLoadedMessages.current = true
      populateMessages(cachedSession.messages)
      loadBarrier.resolve()
      return
    }

    // 否则从 API 加载
    async function loadSessionMessages() {
      try {
        const res = await api.session.getSession(id!)
        const session = res.data

        if (
          previousIdRef.current === loadId &&
          session &&
          session.messages &&
          session.messages.length > 0
        ) {
          hasLoadedMessages.current = true
          populateMessages(session.messages)
        }
      } catch {
        // 忽略：历史消息加载失败由 UI 兜底
      } finally {
        if (previousIdRef.current === loadId) {
          hasLoadedMessages.current = true
        }
        loadBarrier.resolve()
      }
    }

    loadSessionMessages()
  }, [id, chat])

  // 加载并恢复研究检查点状态
  useEffect(() => {
    if (!id || hasLoadedCheckpoint.current) return
    const loadId = id
    const loadBarrier = messagesLoadBarrierRef.current

    async function loadCheckpoint() {
      try {
        let checkpointResponse:
          | Awaited<ReturnType<typeof api.session.getFullResearchCheckpoint>>
          | undefined
        let checkpointError: unknown
        const checkpointRequest = api.session
          .getFullResearchCheckpoint(id!)
          .then((response) => {
            checkpointResponse = response
          })
          .catch((error) => {
            checkpointError = error
          })
        await loadBarrier.promise
        await checkpointRequest
        if (checkpointError) throw checkpointError
        if (previousIdRef.current !== loadId) return
        const res = checkpointResponse
        const response = res?.data
        if (response?.success && response?.checkpoint) {
          const checkpoint = response.checkpoint

          // 只恢复已完成或正在运行的研究
          const checkpointState = checkpoint.state_json as
            | {
                outline_revision?: string
                outline?: OutlinePendingApprovalEvent['sections']
                research_questions?: OutlinePendingApprovalEvent['research_questions']
              }
            | undefined
          if (
            checkpoint.phase === 'awaiting_outline_approval' &&
            checkpointState?.outline_revision
          ) {
            hasLoadedCheckpoint.current = true
            let approvalTarget = chat.list
              .filter((item) => item.role === ChatRole.Assistant)
              .pop()
            if (!approvalTarget) {
              approvalTarget = {
                id: createChatId(),
                role: ChatRole.Assistant,
                type: ChatType.Deepsearch,
                content: '',
              }
              chat.list.push(approvalTarget)
            }
            approvalTarget.type = ChatType.Deepsearch
            setCurrentChatItem(approvalTarget)
            const emptyStream = new ReadableStream<Uint8Array>({
              start(controller) {
                controller.close()
              },
            })
            await sendChat(
              approvalTarget,
              checkpoint.query || '',
              undefined,
              emptyStream,
            )
            setPendingOutline({
              type: 'outline_pending_approval',
              session_id: checkpoint.session_id,
              outline_revision: checkpointState.outline_revision,
              sections: checkpointState.outline || [],
              research_questions: checkpointState.research_questions || [],
            })
            return
          }

          if (
            checkpoint.status === 'completed' ||
            checkpoint.status === 'running' ||
            checkpoint.status === 'paused'
          ) {
            hasLoadedCheckpoint.current = true

            // 恢复 UI 状态
            const uiState = checkpoint.ui_state_json
            // `state_json` 是后端自由结构（`Record<string, unknown>`）：取值处逐个收窄，
            // 完整收窄需重写整条 checkpoint 恢复链（超出本票范围）。此处不再用 `any`。
            const stateJson = checkpoint.state_json


            // 恢复研究步骤 - 如果没有步骤数据，创建默认步骤
            let steps: ResearchStep[] = []
            if (uiState?.research_steps && uiState.research_steps.length > 0) {
              steps = uiState.research_steps.map((s) => ({
                id: (s.type as string) || `step_${Date.now()}`,
                type: s.type as ResearchStep['type'],
                title: (s.type as string) || '',
                subtitle: (s.subtitle as string) || '',
                status: checkpoint.status === 'completed' ? 'completed' : ((s.status as ResearchStep['status']) || 'completed'),
                stats: s.stats as ResearchStep['stats'],
              }))
            } else {
              // 创建默认研究步骤（基于可用数据推断）
              const defaultSteps: ResearchStep['type'][] = ['planning', 'researching', 'analyzing', 'writing']
              if (checkpoint.phase === 'reviewing') defaultSteps.push('reviewing')
              steps = defaultSteps.map(type => ({
                id: type,
                type,
                title: type,
                subtitle: '',
                status: 'completed' as const,
              }))
            }

            setResearchSteps(steps)
            researchStepsRef.current = steps

            // 初始化详情数据 - 使用 stepType 作为 key
            steps.forEach(step => {
              const detail: ResearchDetailData = {
                stepId: step.type,  // 使用 type 作为 ID
                stepType: step.type,
                title: step.title || step.type,
                searchResults: [],
                charts: [],
              }
              researchDetailsRef.current.set(step.type, detail)  // 使用 type 作为 key
            })

            if (uiState) {

              // 恢复搜索结果 - 使用 stepType 作为 key
              if (uiState.search_results && uiState.search_results.length > 0) {
                const searchingType = researchDetailsRef.current.has('searching') ? 'searching' : 'researching'
                const detail = researchDetailsRef.current.get(searchingType)
                if (detail) {
                  detail.searchResults = (uiState.search_results as Record<string, unknown>[]).map((r, i) => ({
                    id: (r.id as string) || `sr_${i}`,
                    title: (r.title as string) || (r.source_name as string) || '',
                    source: (r.source as string) || 'web',
                    url: (r.url as string) || (r.source_url as string) || '',
                    snippet: (r.snippet as string) || (r.content as string) || '',
                    date: (r.date as string) || '',
                  }))
                }
              }

              // 恢复知识图谱 - 使用 stepType 作为 key
              const knowledgeGraph = uiState.knowledge_graph as ResearchDetailData['knowledgeGraph']
              if (knowledgeGraph && (knowledgeGraph.nodes?.length > 0 || knowledgeGraph.edges?.length > 0)) {
                const targetType = researchDetailsRef.current.has('analyzing') ? 'analyzing'
                  : researchDetailsRef.current.has('researching') ? 'researching' : 'searching'
                const detail = researchDetailsRef.current.get(targetType)
                if (detail) {
                  detail.knowledgeGraph = knowledgeGraph
                }
              }

              // 恢复图表 - 使用 stepType 作为 key
              if (uiState.charts && uiState.charts.length > 0) {
                const detail = researchDetailsRef.current.get('analyzing')
                if (detail) {
                  detail.charts = uiState.charts as ResearchDetailData['charts']
                }
              }

              // 恢复报告 - 使用 stepType 作为 key
              if (uiState.streaming_report || checkpoint.final_report) {
                const detail = researchDetailsRef.current.get('writing')
                if (detail) {
                  detail.streamingReport = uiState.streaming_report || checkpoint.final_report || ''
                }
              }

            }

            // 触发数据更新
            setResearchDataVersion(v => v + 1)

            let restoredAssistant: API.ChatItem | undefined
            let shouldPersistRestoredAssistant = false
            if (stateJson) {
              if (chat.list.length === 0) {
                chat.list.push({
                  id: createChatId(),
                  role: ChatRole.User,
                  type: ChatType.Normal,
                  content: checkpoint.query || '',
                })
              }

              let latestUserIndex = -1
              for (let index = chat.list.length - 1; index >= 0; index -= 1) {
                if (chat.list[index].role === ChatRole.User) {
                  latestUserIndex = index
                  break
                }
              }
              restoredAssistant = chat.list
                .slice(latestUserIndex + 1)
                .filter((item) => item.role === ChatRole.Assistant)
                .pop()
              if (!restoredAssistant) {
                shouldPersistRestoredAssistant = true
                chat.list.push({
                  id: createChatId(),
                  role: ChatRole.Assistant,
                  type: ChatType.Deepsearch,
                  content: '',
                })
                restoredAssistant = chat.list[chat.list.length - 1]
              }

              const restoredReport =
                restoredAssistant.content ||
                checkpoint.final_report ||
                uiState?.streaming_report ||
                ''
              if (restoredReport) {
                restoredAssistant.content = restoredReport
                const writingDetail = researchDetailsRef.current.get('writing')
                if (writingDetail) {
                  writingDetail.streamingReport = restoredReport
                }
              }
              restoredAssistant.type = ChatType.Deepsearch
              restoredAssistant.reactMode = true
              restoredAssistant.charts =
                (uiState?.charts as API.ChatItem['charts'] | undefined) ||
                (stateJson.charts as API.ChatItem['charts'] | undefined) ||
                []

              const refs =
                (uiState?.references as Record<string, unknown>[] | undefined) ||
                (stateJson.references as Record<string, unknown>[] | undefined) ||
                []
              if (refs.length > 0 && !restoredAssistant.reference?.length) {
                restoredAssistant.reference = refs.map((ref: Record<string, unknown>, i: number) => ({
                  id: i + 1,
                  title: (ref.title as string) || (ref.source_name as string) || '来源',
                  link: (ref.link as string) || (ref.url as string) || (ref.source_url as string) || '',
                  content: (ref.content as string) || (ref.summary as string) || '',
                  source:
                    ref.source === 'knowledge' || ref.source_type === 'local'
                      ? 'knowledge'
                      : 'web',
                }))
              }

              setCurrentChatItem(restoredAssistant)
            }

            if (checkpoint.status !== 'completed' && restoredAssistant) {
                const resumeResponse = await api.session.resumeResearch(id!)
                if (previousIdRef.current !== loadId) return
                await sendChat(
                  restoredAssistant,
                  checkpoint.query || '',
                  undefined,
                  resumeResponse.data as ReadableStream<Uint8Array>,
                  loadId,
                )
            }
            if (previousIdRef.current !== loadId) return
            if (
              shouldPersistRestoredAssistant &&
              restoredAssistant?.content
            ) {
              try {
                await api.session.addMessage(id!, {
                  role: 'assistant',
                  content: restoredAssistant.content,
                  thinking: restoredAssistant.think,
                  references_data: restoredAssistant.reference
                    ? { references: restoredAssistant.reference }
                    : undefined,
                })
              } catch (error) {
                console.error('[恢复状态] 保存恢复后的助手消息失败:', error)
              }
            }
          }
        }
      } catch {
        // 忽略：检查点恢复失败时静默，界面按普通会话兜底
      }
    }

    loadCheckpoint()
  }, [id, chat, sendChat])

  useEffect(() => {
    if (ctx?.data?.message && !hasSentInitialMessage.current) {
      hasSentInitialMessage.current = true
      send(ctx.data.message, ctx.data.attachmentIds)
    }
  }, [ctx, send])

  useEffect(() => {
    const handleScroll = () => {
      const anchors: {
        id: string
        top: number
        item: API.ChatItem
      }[] = []

      chat.list
        .filter((o) => o.type === ChatType.Deepsearch)
        .forEach((item, index) => {
          const id = createChatIdText(item.id)
          const dom = document.getElementById(id)
          if (!dom) return

          const top = dom.offsetTop
          if (index === 0 || top < window.scrollY) {
            anchors.push({ id, top, item })
          }
        })

      if (anchors.length) {
        const current = anchors.reduce((prev, curr) =>
          curr.top > prev.top ? curr : prev,
        )

        setCurrentChatItem(current.item)
      }
    }

    window.addEventListener('scroll', handleScroll)

    return () => {
      window.removeEventListener('scroll', handleScroll)
    }
  }, [chat.list])

  // 处理步骤点击，切换显示详情 (旧版)
  const handleStepClick = useCallback((stepId: string) => {
    const detail = stepDetailsRef.current.get(stepId)
    if (detail) {
      setSelectedStepDetail(detail)
    }
  }, [])

  // 处理研究步骤点击 (新版)
  const handleResearchStepClick = useCallback((stepId: string) => {
    const detail = researchDetailsRef.current.get(stepId)
    if (detail) {
      setSelectedResearchDetail(detail)
    }
  }, [])

  // 判断是否在深度研究模式（只要是 Deepsearch 类型就启用宽布局）
  const isDeepResearchMode = currentChatItem?.type === ChatType.Deepsearch

  // 聚合所有研究步骤的数据，用于在tab中显示完整信息
  const aggregatedResearchData = useMemo(() => {

    if (!isDeepResearchMode || researchDetailsRef.current.size === 0) {
      return null
    }

    // 从所有步骤中收集数据
    let allSearchResults: ResearchDetailData['searchResults'] = []
    let knowledgeGraph: ResearchDetailData['knowledgeGraph'] = undefined
    let allCharts: ResearchDetailData['charts'] = []
    let streamingReport = ''
    let allSections: ResearchDetailData['sections'] = []

    researchDetailsRef.current.forEach((detail) => {

      // 收集搜索结果
      if (detail.searchResults && detail.searchResults.length > 0) {
        allSearchResults = [...allSearchResults!, ...detail.searchResults]
      }
      // 取最新的知识图谱
      if (detail.knowledgeGraph) {
        knowledgeGraph = detail.knowledgeGraph
      }
      // 收集图表
      if (detail.charts && detail.charts.length > 0) {
        allCharts = [...allCharts!, ...detail.charts]
      }
      // 取最新的流式报告
      if (detail.streamingReport) {
        streamingReport = detail.streamingReport
      }
      // 收集章节草稿
      if (detail.sections && detail.sections.length > 0) {
        allSections = [...allSections!, ...detail.sections]
      }
    })


    // 创建聚合的数据对象
    const aggregated: ResearchDetailData = {
      stepId: selectedResearchDetail?.stepId || 'aggregated',
      stepType: selectedResearchDetail?.stepType || 'aggregated',
      title: selectedResearchDetail?.title || '研究详情',
      subtitle: selectedResearchDetail?.subtitle,
      searchResults: allSearchResults,
      knowledgeGraph,
      charts: allCharts,
      streamingReport,
      sections: allSections,
    }

    return aggregated
    // researchSteps / researchDataVersion 是「ref 内容变化」的显式失效信号：本 memo 读的是
    // researchDetailsRef.current（ref 不参与依赖追踪），必须靠它们触发重算，故刻意保留。
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 见上，非多余依赖
  }, [isDeepResearchMode, selectedResearchDetail, researchSteps, researchDataVersion])

  // 确定右侧面板显示内容
  const rightPanelContent = useMemo(() => {
    if (pendingOutline) {
      return (
        <OutlineApprovalPanel
          sessionId={pendingOutline.session_id}
          outlineRevision={pendingOutline.outline_revision}
          initialSections={pendingOutline.sections}
          initialResearchQuestions={pendingOutline.research_questions}
          approving={approvingOutline}
          error={outlineApprovalError}
          onApprove={handleApproveOutline}
        />
      )
    }
    // 新版: 深度研究模式，显示研究详情面板
    if (isDeepResearchMode) {
      return (
        <ResearchDetail
          data={aggregatedResearchData}
          steps={researchSteps}
          diagnostics={diagnostics}
          diagnosticsLoading={diagnosticsLoading}
          diagnosticsError={diagnosticsError}
          onRefreshDiagnostics={loadDiagnostics}
          onStepClick={handleResearchStepClick}
        />
      )
    }
    // 旧版: 如果当前在深度搜索模式且有步骤详情，显示旧的步骤详情面板
    if (currentChatItem?.type === ChatType.Deepsearch && (selectedStepDetail || currentChatItem?.reactSteps?.length)) {
      return <StepDetailPanel detail={selectedStepDetail} />
    }
    // 否则显示搜索来源
    if (currentChatItem?.search_results?.length) {
      return (
        <Drawer title="搜索来源">
          <Source list={currentChatItem.search_results} />
        </Drawer>
      )
    }
    return null
  }, [
    aggregatedResearchData,
    approvingOutline,
    currentChatItem,
    handleApproveOutline,
    handleResearchStepClick,
    isDeepResearchMode,
    outlineApprovalError,
    pendingOutline,
    diagnostics,
    diagnosticsError,
    diagnosticsLoading,
    loadDiagnostics,
    researchSteps,
    selectedStepDetail,
  ])

  return (
    <ComPageLayout
      sender={
        <>
          <ComSender
            loading={loading}
            attachments={attachments}
            onSend={send}
            onStop={handleStop}
            onUploadAttachment={handleUploadAttachment}
            onRemoveAttachment={handleRemoveAttachment}
          />
        </>
      }
      right={rightPanelContent}
      wideRight={isDeepResearchMode}
    >
      <div className={styles['chat-page']}>
        <ChatMessage list={list} onSend={send} onStepClick={handleStepClick} />
      </div>
    </ComPageLayout>
  )
}
