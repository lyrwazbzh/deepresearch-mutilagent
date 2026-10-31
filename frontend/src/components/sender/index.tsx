/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import IconFile from '@/assets/component/file.svg'
import IconSend from '@/assets/component/send.svg'
import { deviceActions, deviceState, SearchMode } from '@/store/device'
import { knowledgeActions, knowledgeState } from '@/store/knowledge'
import type { KnowledgeBase } from '@/api/knowledge'
import { Button, Input, Space, Dropdown, Checkbox, Tag, Select } from 'antd'
import { FileOutlined, LoadingOutlined, SyncOutlined, CheckCircleOutlined, SearchOutlined, DownOutlined } from '@ant-design/icons'
import classNames from 'classnames'
import { PropsWithChildren, useState, useRef } from 'react'
import { useSnapshot } from 'valtio'
import { Attachment } from '@/api/session'
import './index.scss'

export interface AttachmentInfo {
  id: string
  filename: string
  status: 'uploading' | 'pending' | 'processing' | 'completed' | 'failed'
  file?: File
}

export default function ComSender(
  props: PropsWithChildren<{
    className?: string
    loading?: boolean
    attachments?: AttachmentInfo[]
    onSend?: (value: string, attachmentIds?: string[]) => void | Promise<void>
    onStop?: () => void | Promise<void>
    onUploadAttachment?: (file: File) => Promise<Attachment | null>
    onRemoveAttachment?: (id: string) => void
  }>,
) {
  const {
    className,
    onSend,
    onStop,
    loading,
    attachments = [],
    onUploadAttachment,
    onRemoveAttachment,
    ...rest
  } = props
  const [value, setValue] = useState('')
  const device = useSnapshot(deviceState)
  const knowledge = useSnapshot(knowledgeState)
  const kbList = knowledge.knowledgeBases as KnowledgeBase[]
  const isLocalMode = (device.searchModes as SearchMode[]).includes('local')
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 勾选「本地知识库」时若尚未选库，默认取第一个。
  // 后端在 kb_name 为空时会**静默跳过**本地检索（零结果且无报错），
  // 只勾模式不选库等于什么都没搜（缺陷 P-15）。
  function handleToggleLocal() {
    const turningOn = !(device.searchModes as SearchMode[]).includes('local')
    deviceActions.toggleSearchMode('local')
    if (turningOn && !device.kbName && kbList.length > 0) {
      deviceActions.setKbName(kbList[0].name)
    }
  }

  async function send() {
    if (loading) return
    if (!value && attachments.length === 0) return

    // 过滤出已完成的附件
    const completedAttachmentIds = attachments
      .filter(att => att.status === 'completed')
      .map(att => att.id)

    setValue('')
    await onSend?.(value, completedAttachmentIds.length > 0 ? completedAttachmentIds : undefined)
  }

  function handlePressEnter(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter') {
      if (e.shiftKey) {
        return
      } else {
        e.preventDefault()
        send()
      }
    }
  }

  function handleFileClick() {
    fileInputRef.current?.click()
  }

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files
    if (!files || files.length === 0) return

    const file = files[0]
    if (onUploadAttachment) {
      await onUploadAttachment(file)
    }

    // 清空 input 以便可以重复选择同一文件
    e.target.value = ''
  }

  function getStatusColor(status: string) {
    switch (status) {
      case 'uploading':
      case 'pending':
      case 'processing':
        return 'processing'
      case 'completed':
        return 'success'
      case 'failed':
        return 'error'
      default:
        return 'default'
    }
  }

  function getStatusText(status: string) {
    switch (status) {
      case 'uploading':
        return '上传中'
      case 'pending':
        return '等待处理'
      case 'processing':
        return '处理中'
      case 'completed':
        return '就绪'
      case 'failed':
        return '失败'
      default:
        return ''
    }
  }

  return (
    <div className={classNames('com-sender', className)} {...rest}>
      {/* 附件列表 */}
      {attachments.length > 0 && (
        <div className="com-sender__attachments">
          {attachments.map((att) => (
            <Tag
              key={att.id}
              color={getStatusColor(att.status)}
              closable
              onClose={() => onRemoveAttachment?.(att.id)}
              className="com-sender__attachment-tag"
            >
              {att.status === 'uploading' ? (
                <LoadingOutlined style={{ marginRight: 4 }} />
              ) : att.status === 'pending' || att.status === 'processing' ? (
                <SyncOutlined spin style={{ marginRight: 4 }} />
              ) : att.status === 'completed' ? (
                <CheckCircleOutlined style={{ marginRight: 4 }} />
              ) : (
                <FileOutlined style={{ marginRight: 4 }} />
              )}
              <span className="attachment-filename">{att.filename}</span>
              <span className="attachment-status">({getStatusText(att.status)})</span>
            </Tag>
          ))}
        </div>
      )}

      <Input.TextArea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="按 Enter 发送，Shift + Enter 换行"
        autoSize={{ minRows: 2 }}
        autoFocus
        onPressEnter={handlePressEnter}
      />

      <div className="com-sender__actions">
        <Space className="com-sender__actions-left" size={12}>
          <input
            ref={fileInputRef}
            type="file"
            style={{ display: 'none' }}
            onChange={handleFileChange}
            accept=".pdf,.docx,.doc,.txt,.md,.html,.xlsx,.xls,.pptx,.ppt,.jpg,.jpeg,.png,.gif,.webp,.bmp,.py,.js,.ts,.json,.yaml,.yml,.xml,.csv"
          />
          <Button variant="text" color="default" onClick={handleFileClick}>
            <img src={IconFile} />
            附件
          </Button>

          <Dropdown
            trigger={['click']}
            onOpenChange={(open) => {
              // 打开时才拉取知识库列表，避免每次渲染都发请求
              if (open && kbList.length === 0) knowledgeActions.fetchKnowledgeBases()
            }}
            dropdownRender={() => (
              <div className="com-sender__search-dropdown">
                <Checkbox
                  checked={(device.searchModes as SearchMode[]).includes('web')}
                  onChange={() => deviceActions.toggleSearchMode('web')}
                >
                  深度搜索（网络）
                </Checkbox>
                <Checkbox
                  checked={isLocalMode}
                  onChange={handleToggleLocal}
                >
                  本地知识库
                </Checkbox>
                {isLocalMode && (
                  kbList.length > 0 ? (
                    <Select
                      size="small"
                      className="com-sender__kb-select"
                      placeholder="选择知识库"
                      value={(device.kbName as string) || undefined}
                      onChange={(value: string) => deviceActions.setKbName(value)}
                      options={kbList.map((kb) => ({ label: kb.name, value: kb.name }))}
                    />
                  ) : (
                    <div className="com-sender__kb-empty">
                      {knowledge.loading ? '加载知识库…' : '暂无知识库，请先在「知识库」页创建'}
                    </div>
                  )
                )}
              </div>
            )}
          >
            <Button
              type={(device.searchModes as SearchMode[]).length > 0 ? 'primary' : 'default'}
              ghost={(device.searchModes as SearchMode[]).length > 0}
              icon={<SearchOutlined />}
            >
              {(device.searchModes as SearchMode[]).length === 0 ? '搜索模式' :
               (device.searchModes as SearchMode[]).length === 2 ? '全部搜索' :
               (device.searchModes as SearchMode[]).includes('web') ? '深度搜索' : '知识库'}
              <DownOutlined style={{ fontSize: 10, marginLeft: 4 }} />
            </Button>
          </Dropdown>
        </Space>

        <Space className="com-sender__actions-right" size={12}>
          {loading ? (
            <Button
              color="default"
              variant="filled"
              shape="circle"
              onClick={onStop}
              className="com-sender__stop-btn"
              title="停止生成"
            >
              <span style={{
                display: 'inline-block',
                width: 12,
                height: 12,
                backgroundColor: '#ff4d4f',
                borderRadius: 2,
              }} />
            </Button>
          ) : (
            <Button
              color="default"
              variant="text"
              shape="circle"
              onClick={send}
            >
              <img src={IconSend} />
            </Button>
          )}
        </Space>
      </div>
    </div>
  )
}
