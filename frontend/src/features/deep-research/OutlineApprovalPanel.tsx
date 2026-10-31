import {
  Alert,
  Button,
  Card,
  Checkbox,
  Input,
  Select,
  Typography,
} from 'antd'
import { useEffect, useRef, useState } from 'react'

import {
  clearOutlineDraft,
  loadOutlineDraft,
  saveOutlineDraft,
} from './outline-draft'
import type {
  EditableResearchPlan,
  OutlineSection,
  OutlineSectionType,
  ResearchQuestion,
} from './types'
import styles from './OutlineApprovalPanel.module.scss'

const { Paragraph, Text, Title } = Typography
const CHECKPOINT_ID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function displayError(error?: string): string | undefined {
  if (!error) return undefined
  if (CHECKPOINT_ID_PATTERN.test(error.trim())) {
    return '未找到可审核的研究大纲，请重新发起深度研究。'
  }
  return error
}

export interface OutlineApprovalPanelProps {
  sessionId: string
  outlineRevision: string
  initialSections: OutlineSection[]
  initialResearchQuestions: ResearchQuestion[]
  approving: boolean
  error?: string
  onApprove(plan: EditableResearchPlan): Promise<void>
}

function createId(prefix: string): string {
  const id = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return `${prefix}-${id}`
}

function clonePlan(
  sections: OutlineSection[],
  researchQuestions: ResearchQuestion[],
): EditableResearchPlan {
  return {
    sections: sections.map((section) => ({ ...section })),
    researchQuestions: researchQuestions.map((question) => ({ ...question })),
  }
}

function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (to < 0 || to >= items.length) return items
  const result = [...items]
  const [item] = result.splice(from, 1)
  result.splice(to, 0, item)
  return result
}

function planIsValid(plan: EditableResearchPlan): boolean {
  const sectionsValid =
    plan.sections.length >= 3 &&
    plan.sections.length <= 12 &&
    plan.sections.every(
      (section) => section.title.trim() && section.description.trim(),
    )
  const questionsValid =
    plan.researchQuestions.length >= 3 &&
    plan.researchQuestions.length <= 12 &&
    plan.researchQuestions.every((question) => question.text.trim())
  return Boolean(sectionsValid && questionsValid)
}

export function OutlineApprovalPanel({
  sessionId,
  outlineRevision,
  initialSections,
  initialResearchQuestions,
  approving,
  error,
  onApprove,
}: OutlineApprovalPanelProps) {
  const [plan, setPlan] = useState<EditableResearchPlan>(() => {
    const draft = loadOutlineDraft(sessionId, outlineRevision)
    return draft ?? clonePlan(initialSections, initialResearchQuestions)
  })
  const [submitting, setSubmitting] = useState(false)
  const approvedRef = useRef(false)
  const disabled = approving || submitting
  const valid = planIsValid(plan)
  const visibleError = displayError(error)

  useEffect(() => {
    approvedRef.current = false
  }, [sessionId, outlineRevision])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (!approvedRef.current) {
        saveOutlineDraft(sessionId, outlineRevision, plan)
      }
    }, 800)
    return () => window.clearTimeout(timer)
  }, [outlineRevision, plan, sessionId])

  const updateSection = (index: number, patch: Partial<OutlineSection>) => {
    setPlan((current) => ({
      ...current,
      sections: current.sections.map((section, sectionIndex) =>
        sectionIndex === index ? { ...section, ...patch } : section,
      ),
    }))
  }

  const updateQuestion = (index: number, text: string) => {
    setPlan((current) => ({
      ...current,
      researchQuestions: current.researchQuestions.map((question, questionIndex) =>
        questionIndex === index ? { ...question, text } : question,
      ),
    }))
  }

  const addSection = () => {
    if (plan.sections.length >= 12) return
    setPlan((current) => ({
      ...current,
      sections: [
        ...current.sections,
        {
          id: createId('section'),
          title: '',
          description: '',
          section_type: 'mixed',
          requires_data: false,
          requires_chart: false,
        },
      ],
    }))
  }

  const addQuestion = () => {
    if (plan.researchQuestions.length >= 12) return
    setPlan((current) => ({
      ...current,
      researchQuestions: [
        ...current.researchQuestions,
        { id: createId('question'), text: '' },
      ],
    }))
  }

  const approve = async () => {
    if (!valid || disabled) return
    setSubmitting(true)
    try {
      await onApprove(plan)
      approvedRef.current = true
      clearOutlineDraft(sessionId, outlineRevision)
    } catch {
      // The parent renders the API error and the local draft is preserved.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section
      className={styles.workspace}
      aria-label="研究大纲审核工作区"
    >
      <header className={styles.header}>
        <Text className={styles.eyebrow}>深度研究 · 待确认</Text>
        <Title level={3} className={styles.title}>
          审核研究大纲
        </Title>
        <Paragraph className={styles.description}>
          调整章节和研究问题，确认后开始深度研究。报告至少包含 3 个章节。
        </Paragraph>
      </header>

      <div className={styles.scrollArea}>
        <div className={styles.content}>
          {visibleError ? (
            <Alert
              className={styles.alert}
              type="error"
              showIcon
              message={visibleError}
            />
          ) : null}

          <section className={styles.editorSection}>
            <div className={styles.sectionHeading}>
              <div>
                <Title level={4}>报告章节</Title>
                <Text type="secondary">定义报告的结构、证据类型和图表需求</Text>
              </div>
              <Text className={styles.count}>{plan.sections.length}/12</Text>
            </div>

            <div className={styles.chapterGrid}>
              {plan.sections.map((section, index) => (
                <Card
                  key={section.id}
                  className={styles.chapterCard}
                  size="small"
                  title={`第 ${index + 1} 章`}
                >
                  <div className={styles.cardFields}>
                    <Input
                      aria-label={`第 ${index + 1} 章标题`}
                      disabled={disabled}
                      value={section.title}
                      placeholder="输入章节标题"
                      onChange={(event) =>
                        updateSection(index, { title: event.target.value })
                      }
                    />
                    <Input.TextArea
                      aria-label={`第 ${index + 1} 章研究内容`}
                      disabled={disabled}
                      value={section.description}
                      placeholder="说明该章需要研究的内容"
                      autoSize={{ minRows: 3, maxRows: 7 }}
                      onChange={(event) =>
                        updateSection(index, { description: event.target.value })
                      }
                    />
                    <div className={styles.metaRow}>
                      <Select<OutlineSectionType>
                        className={styles.typeSelect}
                        aria-label={`第 ${index + 1} 章类型`}
                        disabled={disabled}
                        value={section.section_type}
                        options={[
                          { value: 'qualitative', label: '定性分析' },
                          { value: 'quantitative', label: '定量分析' },
                          { value: 'mixed', label: '综合分析' },
                        ]}
                        onChange={(section_type) =>
                          updateSection(index, { section_type })
                        }
                      />
                      <Checkbox
                        disabled={disabled}
                        checked={section.requires_data}
                        onChange={(event) =>
                          updateSection(index, {
                            requires_data: event.target.checked,
                          })
                        }
                      >
                        需要数据
                      </Checkbox>
                      <Checkbox
                        disabled={disabled}
                        checked={section.requires_chart}
                        onChange={(event) =>
                          updateSection(index, {
                            requires_chart: event.target.checked,
                          })
                        }
                      >
                        需要图表
                      </Checkbox>
                    </div>
                    <div className={styles.actionRow}>
                      <Button
                        aria-label={`上移第 ${index + 1} 章`}
                        disabled={disabled || index === 0}
                        onClick={() =>
                          setPlan((current) => ({
                            ...current,
                            sections: moveItem(
                              current.sections,
                              index,
                              index - 1,
                            ),
                          }))
                        }
                      >
                        上移
                      </Button>
                      <Button
                        aria-label={`下移第 ${index + 1} 章`}
                        disabled={
                          disabled || index === plan.sections.length - 1
                        }
                        onClick={() =>
                          setPlan((current) => ({
                            ...current,
                            sections: moveItem(
                              current.sections,
                              index,
                              index + 1,
                            ),
                          }))
                        }
                      >
                        下移
                      </Button>
                      <Button
                        danger
                        aria-label={`删除第 ${index + 1} 章`}
                        disabled={disabled || plan.sections.length <= 3}
                        onClick={() =>
                          setPlan((current) => ({
                            ...current,
                            sections: current.sections.filter(
                              (_, sectionIndex) => sectionIndex !== index,
                            ),
                          }))
                        }
                      >
                        删除
                      </Button>
                    </div>
                  </div>
                </Card>
              ))}
            </div>

            <Button
              className={styles.addButton}
              aria-label="添加章节"
              disabled={disabled || plan.sections.length >= 12}
              onClick={addSection}
            >
              + 添加章节
            </Button>
            {plan.sections.some(
              (section) =>
                !section.title.trim() || !section.description.trim(),
            ) ? (
              <Text type="danger" className={styles.validation}>
                章节标题和研究内容不能为空
              </Text>
            ) : null}
          </section>

          <section className={styles.editorSection}>
            <div className={styles.sectionHeading}>
              <div>
                <Title level={4}>研究问题</Title>
                <Text type="secondary">这些问题将用于指导资料搜索和证据分析</Text>
              </div>
              <Text className={styles.count}>
                {plan.researchQuestions.length}/12
              </Text>
            </div>

            <div className={styles.questionList}>
              {plan.researchQuestions.map((question, index) => (
                <div key={question.id} className={styles.questionRow}>
                  <div className={styles.questionField}>
                    <Text className={styles.questionNumber}>
                      问题 {index + 1}
                    </Text>
                    <Input.TextArea
                      aria-label={`研究问题 ${index + 1}`}
                      disabled={disabled}
                      value={question.text}
                      placeholder="输入需要回答的研究问题"
                      autoSize={{ minRows: 2, maxRows: 5 }}
                      onChange={(event) =>
                        updateQuestion(index, event.target.value)
                      }
                    />
                  </div>
                  <div className={styles.actionRow}>
                    <Button
                      aria-label={`上移研究问题 ${index + 1}`}
                      disabled={disabled || index === 0}
                      onClick={() =>
                        setPlan((current) => ({
                          ...current,
                          researchQuestions: moveItem(
                            current.researchQuestions,
                            index,
                            index - 1,
                          ),
                        }))
                      }
                    >
                      上移
                    </Button>
                    <Button
                      aria-label={`下移研究问题 ${index + 1}`}
                      disabled={
                        disabled ||
                        index === plan.researchQuestions.length - 1
                      }
                      onClick={() =>
                        setPlan((current) => ({
                          ...current,
                          researchQuestions: moveItem(
                            current.researchQuestions,
                            index,
                            index + 1,
                          ),
                        }))
                      }
                    >
                      下移
                    </Button>
                    <Button
                      danger
                      aria-label={`删除研究问题 ${index + 1}`}
                      disabled={
                        disabled || plan.researchQuestions.length <= 3
                      }
                      onClick={() =>
                        setPlan((current) => ({
                          ...current,
                          researchQuestions:
                            current.researchQuestions.filter(
                              (_, questionIndex) => questionIndex !== index,
                            ),
                        }))
                      }
                    >
                      删除
                    </Button>
                  </div>
                </div>
              ))}
            </div>

            <Button
              className={styles.addButton}
              aria-label="添加研究问题"
              disabled={disabled || plan.researchQuestions.length >= 12}
              onClick={addQuestion}
            >
              + 添加研究问题
            </Button>
            {plan.researchQuestions.some(
              (question) => !question.text.trim(),
            ) ? (
              <Text type="danger" className={styles.validation}>
                研究问题不能为空
              </Text>
            ) : null}
          </section>
        </div>
      </div>

      <footer className={styles.footer}>
        <div className={styles.footerInner}>
          <Text type={valid ? 'secondary' : 'danger'}>
            {valid
              ? `已准备 ${plan.sections.length} 个章节和 ${plan.researchQuestions.length} 个研究问题`
              : '请先补全所有章节和研究问题'}
          </Text>
          <Button
            className={styles.approveButton}
            type="primary"
            size="large"
            aria-label={disabled ? '正在确认大纲' : '确认大纲并开始研究'}
            loading={disabled}
            disabled={!valid || disabled}
            onClick={approve}
          >
            {disabled ? '正在确认…' : '确认大纲并开始研究'}
          </Button>
        </div>
      </footer>
    </section>
  )
}
