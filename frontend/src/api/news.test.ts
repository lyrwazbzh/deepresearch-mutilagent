/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const requestMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('./request', () => ({ request: requestMocks }))

import { getNewsList, triggerCollection } from './news'

/**
 * T70：`api/news.ts` 曾把 `const res = await …; return res.data` 抄 8 遍 ——
 * 每个函数都得自己知道该 client 的私有约定。收拢后一律经 `request/read-body`
 * 的唯一出口取体。这里从**行为**上守这件事（不数源码出现次数）：
 * 调用方拿到的就是后端返回体本身。
 */
describe('news api 取体（T70 收敛到 read-body）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('列表接口：拿到返回体本身，total / stats 不被吞掉', async () => {
    const body = {
      success: true,
      data: [{ id: 'n1' }],
      total: 1,
      stats: { recent_24h: 1 },
    }
    requestMocks.get.mockResolvedValue({ data: body })

    await expect(getNewsList({ limit: 20 })).resolves.toEqual(body)
    expect(requestMocks.get).toHaveBeenCalledWith('/news/list', {
      params: { limit: 20 },
      loading: false,
    })
  })

  it('POST 采集接口同样经唯一出口取体，且保留其专属 config', async () => {
    const body = {
      success: true,
      message: '已触发',
      news_collected: 3,
      bidding_collected: 1,
      errors: [],
    }
    requestMocks.post.mockResolvedValue({ data: body })

    await expect(triggerCollection({ max_news: 5 })).resolves.toEqual(body)
    expect(requestMocks.post).toHaveBeenCalledWith('/news/collect', null, {
      params: { max_news: 5 },
      loading: false,
      cancelRepeat: false,
      timeout: 120000,
    })
  })
})
