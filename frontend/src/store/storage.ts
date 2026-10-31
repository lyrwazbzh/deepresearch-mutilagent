/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { allKeys, readItem, removeItem, writeItem } from '@/utils/local-storage'
import { ProxyPersistStorageEngine } from './valtio-persist'

// 底层读写统一走 @/utils/local-storage（T37：全站唯一访问点）
const storage: ProxyPersistStorageEngine = {
  getItem: readItem,
  setItem: writeItem,
  removeItem,
  getAllKeys: allKeys,
}

export default storage
