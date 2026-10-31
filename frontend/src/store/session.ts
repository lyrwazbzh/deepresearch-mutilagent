/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { proxy } from 'valtio'
import {
  Session,
  SessionWithMessages,
  getSessions,
  createSession,
  getSession,
  updateSession,
  deleteSession,
} from '@/api/session'

interface SessionState {
  sessions: Session[]
  currentSession: SessionWithMessages | null
  loading: boolean
  error: string | null
}

export const sessionState = proxy<SessionState>({
  sessions: [],
  currentSession: null,
  loading: false,
  error: null,
})

export const sessionActions = {
  async fetchSessions() {
    sessionState.loading = true
    sessionState.error = null
    try {
      const response = await getSessions({ limit: 50 })
      // 本 client 不解包（T70 起为显式契约，见 api/request/request.ts）：response 是
      // AxiosResponse，后端返回体在 response.data。
      // 注：`api/request/read-body` 的 `readBody()` 是 **api 模块层**的取体出口；本 store
      // 属**尚未迁移**的存量调用方（§4 补审 / T72 实测口径，见 read-body.ts 的口径边界）。
      // 故此处直接取 `.data` —— 与 `readBody(getSessions(...))` 行为等价，勿据此认为全仓已收拢。
      const sessions = response.data
      sessionState.sessions = Array.isArray(sessions) ? sessions : []
    } catch (err) {
      sessionState.error = (err as Error).message || '获取会话列表失败'
    } finally {
      sessionState.loading = false
    }
  },

  async createNewSession(title?: string, sessionType: 'chat' | 'deepsearch' = 'chat') {
    try {
      const response = await createSession({ title, session_type: sessionType })
      const newSession = response.data
      sessionState.sessions.unshift(newSession)
      return newSession
    } catch (err) {
      sessionState.error = (err as Error).message || '创建会话失败'
      throw err
    }
  },

  async loadSession(sessionId: string) {
    sessionState.loading = true
    sessionState.error = null
    try {
      const response = await getSession(sessionId)
      const session = response.data
      sessionState.currentSession = session
      return session
    } catch (err) {
      sessionState.error = (err as Error).message || '加载会话失败'
      throw err
    } finally {
      sessionState.loading = false
    }
  },

  async renameSession(sessionId: string, title: string) {
    try {
      const response = await updateSession(sessionId, { title })
      const updatedSession = response.data
      const index = sessionState.sessions.findIndex((s) => s.id === sessionId)
      if (index !== -1) {
        sessionState.sessions[index] = updatedSession
      }
      if (sessionState.currentSession?.id === sessionId) {
        sessionState.currentSession.title = title
      }
      return updatedSession
    } catch (err) {
      sessionState.error = (err as Error).message || '重命名会话失败'
      throw err
    }
  },

  async removeSession(sessionId: string) {
    try {
      await deleteSession(sessionId)
      sessionState.sessions = sessionState.sessions.filter((s) => s.id !== sessionId)
      if (sessionState.currentSession?.id === sessionId) {
        sessionState.currentSession = null
      }
    } catch (err) {
      sessionState.error = (err as Error).message || '删除会话失败'
      throw err
    }
  },

  setCurrentSession(session: SessionWithMessages | null) {
    sessionState.currentSession = session
  },

  clearError() {
    sessionState.error = null
  },
}
