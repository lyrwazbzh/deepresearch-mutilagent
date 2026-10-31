import { beforeEach, describe, expect, it } from 'vitest'

import {
  clearOutlineDraft,
  getOutlineDraftKey,
  loadOutlineDraft,
  saveOutlineDraft,
} from './outline-draft'
import type { EditableResearchPlan } from './types'

const plan: EditableResearchPlan = {
  sections: [1, 2, 3].map((index) => ({
    id: `section-${index}`,
    title: `Chapter ${index}`,
    description: `Description ${index}`,
    section_type: 'mixed',
    requires_data: false,
    requires_chart: false,
  })),
  researchQuestions: [1, 2, 3].map((index) => ({
    id: `question-${index}`,
    text: `Question ${index}`,
  })),
}

describe('outline draft storage', () => {
  beforeEach(() => localStorage.clear())

  it('saves and restores a draft scoped to session and revision', () => {
    saveOutlineDraft('session-1', 'revision-1', plan)

    expect(loadOutlineDraft('session-1', 'revision-1')).toMatchObject(plan)
    expect(loadOutlineDraft('session-2', 'revision-1')).toBeNull()
    expect(loadOutlineDraft('session-1', 'revision-2')).toBeNull()
  })

  it('rejects and removes stale or malformed stored values', () => {
    const key = getOutlineDraftKey('session-1', 'revision-1')
    localStorage.setItem(
      key,
      JSON.stringify({
        outlineRevision: 'revision-old',
        sections: plan.sections,
        researchQuestions: plan.researchQuestions,
        savedAt: Date.now(),
      }),
    )

    expect(loadOutlineDraft('session-1', 'revision-1')).toBeNull()
    expect(localStorage.getItem(key)).toBeNull()

    localStorage.setItem(key, '{bad json')
    expect(loadOutlineDraft('session-1', 'revision-1')).toBeNull()
    expect(localStorage.getItem(key)).toBeNull()
  })

  it('clears an approved draft', () => {
    saveOutlineDraft('session-1', 'revision-1', plan)
    clearOutlineDraft('session-1', 'revision-1')

    expect(loadOutlineDraft('session-1', 'revision-1')).toBeNull()
  })
})
