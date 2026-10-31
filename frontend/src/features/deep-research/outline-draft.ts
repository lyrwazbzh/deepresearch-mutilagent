import type {
  EditableResearchPlan,
  OutlineSection,
  ResearchQuestion,
} from './types'

import { readItem, removeItem, writeItem } from '@/utils/local-storage'

export interface StoredOutlineDraft extends EditableResearchPlan {
  outlineRevision: string
  savedAt: number
}

export function getOutlineDraftKey(
  sessionId: string,
  outlineRevision: string,
): string {
  return `deep-research-outline-draft:${sessionId}:${outlineRevision}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isSection(value: unknown): value is OutlineSection {
  if (!isRecord(value)) return false
  return (
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.description === 'string' &&
    ['qualitative', 'quantitative', 'mixed'].includes(
      String(value.section_type),
    ) &&
    typeof value.requires_data === 'boolean' &&
    typeof value.requires_chart === 'boolean'
  )
}

function isQuestion(value: unknown): value is ResearchQuestion {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.text === 'string'
  )
}

function isDraft(
  value: unknown,
  outlineRevision: string,
): value is StoredOutlineDraft {
  return (
    isRecord(value) &&
    value.outlineRevision === outlineRevision &&
    typeof value.savedAt === 'number' &&
    Array.isArray(value.sections) &&
    value.sections.every(isSection) &&
    Array.isArray(value.researchQuestions) &&
    value.researchQuestions.every(isQuestion)
  )
}

export function saveOutlineDraft(
  sessionId: string,
  outlineRevision: string,
  plan: EditableResearchPlan,
): void {
  const draft: StoredOutlineDraft = {
    outlineRevision,
    sections: plan.sections,
    researchQuestions: plan.researchQuestions,
    savedAt: Date.now(),
  }
  writeItem(
    getOutlineDraftKey(sessionId, outlineRevision),
    JSON.stringify(draft),
  )
}

export function loadOutlineDraft(
  sessionId: string,
  outlineRevision: string,
): StoredOutlineDraft | null {
  const key = getOutlineDraftKey(sessionId, outlineRevision)
  const raw = readItem(key)
  if (!raw) return null

  try {
    const parsed: unknown = JSON.parse(raw)
    if (isDraft(parsed, outlineRevision)) return parsed
  } catch {
    // Invalid local data is discarded below.
  }

  removeItem(key)
  return null
}

export function clearOutlineDraft(
  sessionId: string,
  outlineRevision: string,
): void {
  removeItem(getOutlineDraftKey(sessionId, outlineRevision))
}
