/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import storage from './storage'
import proxyWithPersist, { PersistStrategy } from './valtio-persist'

// 搜索模式类型
export type SearchMode = 'web' | 'local'

const state = proxyWithPersist({
  name: 'device',
  version: 1,
  getStorage: () => storage,
  persistStrategies: {
    searchModes: PersistStrategy.SingleFile,
    kbName: PersistStrategy.SingleFile,
  },
  migrations: {
    // 从 v0 迁移: useDeepsearch -> searchModes
    // 注意：valtio-persist 调用迁移时不传参、忽略返回值（`await migration()`），
    // 迁移拿不到 proxy 入参、返回值也不被消费，因此无法改写已载入的旧持久化数据（机制缺陷，记入 TRACKER 已知残留，待后续票修复）。
    // 原迁移函数以 any 类型的 oldState 入参读取旧状态，运行时该入参恒为 undefined（潜在 TypeError，被 any 掩盖）。
    // 此处收敛为无参 no-op：保证版本簿记正常推进，且不再触碰不存在的入参
    1: (): void => {},
  },

  initialState: {
    chatting: false,
    // 搜索模式: 'web' = 深度搜索(网络), 'local' = 本地知识库
    searchModes: [] as SearchMode[],
    // 本地知识库模式选中的知识库名（后端按集合 kb_<名称> 检索）。
    // 为空时后端会静默跳过本地检索 —— 只勾模式不选库等于什么都没搜（缺陷 P-15）
    kbName: '',
  },
})

const actions = {
  setChatting(chatting: boolean) {
    state.chatting = chatting
  },
  setSearchModes(modes: SearchMode[]) {
    state.searchModes = modes
  },
  toggleSearchMode(mode: SearchMode) {
    const currentModes = state.searchModes as SearchMode[]
    if (currentModes.includes(mode)) {
      state.searchModes = currentModes.filter(m => m !== mode)
    } else {
      state.searchModes = [...currentModes, mode]
    }
  },
  setKbName(kbName: string) {
    state.kbName = kbName
  },
  // 兼容旧代码
  get useDeepsearch() {
    return (state.searchModes as SearchMode[]).includes('web')
  },
  get useLocalKb() {
    return (state.searchModes as SearchMode[]).includes('local')
  },
}

export const deviceState = state
export const deviceActions = actions
