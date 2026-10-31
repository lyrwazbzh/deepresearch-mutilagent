/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import classNames from 'classnames'
import DOMPurify, { type Config as DOMPurifyConfig } from 'dompurify'
import { Marked, Renderer, TokenizerAndRendererExtension } from 'marked'
import { useMemo } from 'react'
import './index.scss'

/**
 * 消毒配置（P-18）：`marked` 自 v5 起已移除内置 `sanitize`，其输出必须自行消毒后才可
 * 交给 `dangerouslySetInnerHTML`。默认白名单已覆盖主要威胁（`<script>`、`on*` 事件处理器、
 * `javascript:` URL），这里只做两件事：
 *
 * - `ADD_ATTR: ['loading']` 放行自定义图片渲染写出的原生懒加载属性；
 * - `ADD_DATA_URI_TAGS: ['img']` 明确允许 `data:image/*` 内联图（图表以 base64 返回）。
 *
 * **不要**用 `ALLOWED_TAGS` 收敛成自定义白名单：正文来自第三方检索网页，标签种类不可预知，
 * 过窄的白名单会把合法内容删成空白；消毒的目标是「去掉可执行的」，不是「只留下我认识的」。
 */
const SANITIZE_CONFIG = {
  ADD_ATTR: ['loading'],
  ADD_DATA_URI_TAGS: ['img'],
  FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form'],
} satisfies DOMPurifyConfig

export default function Markdown(props: {
  className?: string
  value?: string
  extensions?: TokenizerAndRendererExtension[]
}) {
  const { value, extensions, className, ...otherProps } = props

  const html = useMemo(() => {
    const renderer = new Renderer()

    // 自定义图片渲染：只渲染有效的图片 URL，隐藏无效的图片引用
    renderer.image = ({ href, title, text }: { href: string; title: string | null; text: string }) => {
      // 只渲染 base64 data URL 或有效的 http(s) URL
      if (href && (href.startsWith('data:image/') || href.startsWith('http://') || href.startsWith('https://'))) {
        const titleAttr = title ? ` title="${title}"` : ''
        return `<img src="${href}" alt="${text || ''}"${titleAttr} class="markdown-image" loading="lazy" />`
      }
      // 无效的图片 URL，直接隐藏（实际图表在"可视化图表"tab中显示）
      return ''
    }

    const marked = new Marked({
      extensions,
    })
    const raw = marked.parse(value ?? '', {
      gfm: false,
      renderer,
    })

    // `marked.parse` 的声明是 `string | Promise<string>`（只有传异步扩展时才可能返回 Promise）。
    // 本组件只传同步扩展，故实际必为 string —— 但**不能**因此省掉这个分支：Promise 一旦
    // 漏到 `dangerouslySetInnerHTML` 就会被字符串化成 "[object Promise]"，而这里更糟的是
    // 消毒无从施加。宁可渲染空，也不写未消毒的 HTML。
    if (typeof raw !== 'string') {
      return ''
    }

    // 消毒后才写进 DOM。注意：这也覆盖了上面 renderer.image 的属性插值 ——
    // `title` / `text` 里若含 `"` 可闭合属性并注入 `onerror=`，DOMPurify 会在
    // 重新解析 DOM 时把该事件处理器剥掉（见 markdown.test.tsx 的注入用例）。
    return DOMPurify.sanitize(raw, SANITIZE_CONFIG)
  }, [value, extensions])

  return (
    <div
      className={classNames('com-markdown', className)}
      {...otherProps}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
