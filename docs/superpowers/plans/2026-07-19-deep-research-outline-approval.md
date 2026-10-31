# Deep Research Outline Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pause Deep Research V2 after plan generation so an authenticated user can edit and approve 3–12 outline sections and 3–12 research questions before keyword generation and research continue.

**Architecture:** Persist an explicit `awaiting_outline_approval + paused` checkpoint, approve it atomically through a dedicated authenticated SSE endpoint, and resume all research through a phase dispatcher. Stable section/question IDs and an `outline_revision` prevent positional mismatches and stale approval; the frontend shares one SSE consumer between initial and resumed streams.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLAlchemy/PostgreSQL, pytest, React 19, TypeScript, Ant Design 5, Vitest, Testing Library, Playwright.

---

## Baseline and file map

Baseline on 2026-07-19:

- `python -m compileall -q backend/app` passes.
- `npm run lint` has 94 pre-existing errors across unrelated files. Use targeted ESLint for files changed by this plan; retain the full-lint output as a known baseline.
- `npm run build` exceeded 120 seconds without an emitted compiler error. Re-run with a 5-minute timeout after implementation.
- The repository has no existing automated test runner configuration.

New files and responsibilities:

- `backend/pytest.ini`: backend pytest configuration and markers.
- `backend/tests/conftest.py`: test environment and reusable fixtures.
- `backend/tests/service/deep_research_v2/test_architect_planning.py`: outline/search-plan parsing and fallback tests.
- `backend/tests/service/deep_research_v2/test_phase_dispatcher.py`: phase-resume regression tests.
- `backend/tests/service/test_checkpoint_service.py`: ownership, revision, uniqueness, and atomic approval tests.
- `backend/tests/router/test_research_outline_approval.py`: authenticated HTTP/SSE contract tests.
- `backend/tests/evals/test_outline_planning_eval.py`: opt-in live-LLM quality evaluation.
- `backend/migrations/20260719_unique_research_checkpoint_session_id.sql`: duplicate preflight and unique index migration.
- `frontend/vitest.config.ts`: Vitest/jsdom configuration.
- `frontend/src/test/setup.ts`: Testing Library setup and browser API cleanup.
- `frontend/src/features/deep-research/types.ts`: typed research events and editable plan contracts.
- `frontend/src/features/deep-research/research-stream.ts`: reusable SSE consumer.
- `frontend/src/features/deep-research/research-stream.test.ts`: chunking and failure tests.
- `frontend/src/features/deep-research/outline-draft.ts`: revision-scoped localStorage draft helper.
- `frontend/src/features/deep-research/outline-draft.test.ts`: draft lifecycle tests.
- `frontend/src/features/deep-research/OutlineApprovalPanel.tsx`: editable approval UI.
- `frontend/src/features/deep-research/OutlineApprovalPanel.test.tsx`: component behavior tests.
- `frontend/playwright.config.ts`: browser-test configuration.
- `frontend/e2e/deep-research-outline-approval.spec.ts`: complete approval and reconnect flow.

Modified files and responsibilities:

- `backend/requirements.txt`: pytest dependencies.
- `backend/app/models/research.py`: unique session constraint metadata.
- `backend/app/service/deep_research_v2/state.py`: approval phase and revision fields.
- `backend/app/service/deep_research_v2/agents/architect.py`: split outline and search-plan generation.
- `backend/app/service/checkpoint_service.py`: user-scoped access and atomic approval.
- `backend/app/service/deep_research_v2/graph.py`: phase dispatcher and pause/resume boundaries.
- `backend/app/router/research_router.py`: authenticated stream, approve, resume, cancel, and checkpoint routes.
- `frontend/package.json` and `frontend/package-lock.json`: test dependencies and scripts.
- `frontend/src/api/session.ts`: approve/resume streaming APIs and types.
- `frontend/src/pages/chat/index.tsx`: panel state and shared stream integration.

### Task 1: Establish backend test infrastructure and state contracts

**Files:**
- Modify: `backend/requirements.txt`
- Create: `backend/pytest.ini`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/service/deep_research_v2/test_state.py`
- Modify: `backend/app/service/deep_research_v2/state.py`

- [ ] **Step 1: Add backend test dependencies**

Append to `backend/requirements.txt`:

```text
# Testing
pytest>=8.2.0
pytest-asyncio>=0.23.0
pytest-cov>=5.0.0
```

Create `backend/pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
markers =
    integration: requires PostgreSQL and TEST_DATABASE_URL
    llm_eval: calls the configured live LLM
```

Create `backend/tests/conftest.py`:

```python
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("ENV", "test")
```

- [ ] **Step 2: Write failing state tests**

Create `backend/tests/service/deep_research_v2/test_state.py`:

```python
from service.deep_research_v2.state import ResearchPhase, create_initial_state


def test_state_exposes_outline_approval_phase():
    assert ResearchPhase.AWAITING_OUTLINE_APPROVAL.value == "awaiting_outline_approval"


def test_initial_state_has_no_outline_revision():
    state = create_initial_state("topic", "session-1")
    assert state["phase"] == "init"
    assert state["outline_revision"] is None
```

- [ ] **Step 3: Run the tests and verify the expected failure**

Run: `cd backend && python -m pytest tests/service/deep_research_v2/test_state.py -q`

Expected: FAIL because `AWAITING_OUTLINE_APPROVAL` and `outline_revision` do not exist.

- [ ] **Step 4: Add the explicit phase and state field**

In `state.py`, add:

```python
class ResearchPhase(str, Enum):
    INIT = "init"
    AWAITING_OUTLINE_APPROVAL = "awaiting_outline_approval"
    PLANNING = "planning"
    # existing phases remain unchanged


class ResearchState(TypedDict):
    # existing fields remain unchanged
    outline_revision: Optional[str]
```

Set `outline_revision=None` in `create_initial_state()`.

- [ ] **Step 5: Verify and commit**

Run: `cd backend && python -m pytest tests/service/deep_research_v2/test_state.py -q`

Expected: `2 passed`.

Commit:

```bash
git add backend/requirements.txt backend/pytest.ini backend/tests/conftest.py backend/tests/service/deep_research_v2/test_state.py backend/app/service/deep_research_v2/state.py
git commit -m "test: establish deep research state test foundation"
```

### Task 2: Split Architect outline and search-plan generation

**Files:**
- Create: `backend/tests/service/deep_research_v2/test_architect_planning.py`
- Modify: `backend/app/service/deep_research_v2/agents/architect.py`

- [ ] **Step 1: Write failing parser and process-routing tests**

Create test cases that instantiate `ChiefArchitect` with dummy credentials and monkeypatch `call_llm`:

```python
@pytest.mark.asyncio
async def test_init_generates_only_editable_plan(architect, initial_state):
    architect.call_llm = AsyncMock(return_value=json.dumps({
        "sections": [
            {"id": f"section_{i}", "title": f"T{i}", "description": f"D{i}"}
            for i in range(1, 6)
        ],
        "research_questions": [
            {"id": f"question_{i}", "text": f"Q{i}"} for i in range(1, 4)
        ],
    }))
    result = await architect.process(initial_state)
    assert result["phase"] == "awaiting_outline_approval"
    assert result["outline_revision"]
    assert all(section["search_queries"] == [] for section in result["outline"])
    assert result["hypotheses"] == []


@pytest.mark.asyncio
async def test_planning_merges_queries_by_stable_id(architect, approved_state):
    architect.call_llm = AsyncMock(return_value=json.dumps({
        "sections": [{"id": section["id"], "queries": [f"{section['title']} 2026"]}
                     for section in approved_state["outline"]],
        "hypotheses": [{"id": "hypothesis_1", "content": "可验证假设"}],
    }))
    result = await architect.process(approved_state)
    assert result["phase"] == "researching"
    assert all(section["search_queries"] for section in result["outline"])
```

Also cover duplicate IDs, unknown IDs, one automatic retry, at least three valid sections with deterministic fallback, and fewer than three valid sections retaining `phase=planning` with an error.

- [ ] **Step 2: Run the focused tests**

Run: `cd backend && python -m pytest tests/service/deep_research_v2/test_architect_planning.py -q`

Expected: FAIL because the two-stage methods and stable-ID parser are absent.

- [ ] **Step 3: Implement the two-stage Architect contract**

Replace the single planning prompt with `OUTLINE_ONLY_PROMPT` and `SEARCH_PLAN_PROMPT`. Add routing:

```python
async def process(self, state: ResearchState) -> ResearchState:
    if state["phase"] == ResearchPhase.INIT.value:
        return await self._generate_outline(state)
    if state["phase"] == ResearchPhase.PLANNING.value:
        return await self._generate_search_plan(state)
    if state["phase"] == ResearchPhase.REVIEWING.value:
        return await self._check_revision(state)
    return state
```

`_generate_outline()` must normalize 5–8 model sections and 3–6 questions, generate missing stable IDs with `uuid.uuid4().hex`, set empty `search_queries`, set `outline_revision=uuid.uuid4().hex`, and transition to `awaiting_outline_approval`.

`_generate_search_plan()` must serialize the approved sections and questions, validate returned IDs, retry once with validation errors, merge valid queries, and use these deterministic fallbacks for missing sections when at least three sections are valid:

```python
section["search_queries"] = [
    f"{state['query']} {section['title']}",
    f"{section['title']} {section['description']}",
]
```

If fewer than three sections are valid, append a descriptive error and leave `phase=planning`; otherwise set `phase=researching`.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && python -m pytest tests/service/deep_research_v2/test_architect_planning.py -q`

Expected: all tests pass.

Commit:

```bash
git add backend/app/service/deep_research_v2/agents/architect.py backend/tests/service/deep_research_v2/test_architect_planning.py
git commit -m "feat: split outline and search plan generation"
```

### Task 3: Add unique checkpoint migration and atomic approval

**Files:**
- Create: `backend/migrations/20260719_unique_research_checkpoint_session_id.sql`
- Modify: `backend/app/models/research.py`
- Create: `backend/tests/service/test_checkpoint_service.py`
- Modify: `backend/app/service/checkpoint_service.py`

- [ ] **Step 1: Write failing service tests**

Use a mocked SQLAlchemy session for fast unit tests and an `@pytest.mark.integration` PostgreSQL case for real row locking. Implement these named cases with the listed assertions:

```text
test_approve_outline_rejects_wrong_user
  assert CheckpointNotFound and no commit
test_approve_outline_rejects_non_paused_phase
  assert OutlineApprovalConflict and unchanged state_json
test_approve_outline_rejects_stale_revision
  assert OutlineApprovalConflict and unchanged outline_revision
test_approve_outline_validates_three_to_twelve_sections
  assert ValueError for 2 and 13 sections
test_approve_outline_validates_three_to_twelve_questions
  assert ValueError for 2 and 13 questions
test_approve_outline_sets_planning_and_running
  assert returned state phase == "planning", row phase == "planning", row status == "running"
test_concurrent_approval_has_exactly_one_winner [integration]
  create one paused row, approve from two independent DB sessions, assert one success and one OutlineApprovalConflict
```

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && python -m pytest tests/service/test_checkpoint_service.py -q -m "not integration"`

Expected: FAIL because user-scoped access and `approve_outline()` do not exist.

- [ ] **Step 3: Add the safe SQL migration**

Create a PostgreSQL script that first executes:

```sql
SELECT session_id, COUNT(*) AS duplicate_count
FROM research_checkpoints
GROUP BY session_id
HAVING COUNT(*) > 1;
```

Then use this guard to abort if duplicates exist:

```sql
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM research_checkpoints
        GROUP BY session_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'Duplicate research checkpoint session_id values exist; resolve them before adding the unique index';
    END IF;
END
$$;
```

Only after the guard passes, create:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_checkpoints_session_id
ON research_checkpoints (session_id);
```

Do not delete or merge historical rows. Mirror the constraint in `ResearchCheckpoint.__table_args__` with `UniqueConstraint("session_id", name="uq_research_checkpoints_session_id")`.

- [ ] **Step 4: Implement user-scoped atomic approval**

Add explicit exceptions `CheckpointNotFound` and `OutlineApprovalConflict`. Implement:

```python
def approve_outline(
    self,
    session_id: str,
    user_id: str,
    outline_revision: str,
    sections: List[Dict[str, Any]],
    research_questions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Lock and atomically approve one user's paused outline."""
```

The method must query with both `session_id` and `user_id`, call `with_for_update()`, validate phase/status/revision and 3–12 counts, reject blank fields and duplicate IDs, update `state_json`, `phase`, and `status`, commit, and return a detached plain dict. No LLM call occurs inside this method.

Update all checkpoint lookup/mutation helpers used by research routes to accept `user_id` and filter by ownership.

- [ ] **Step 5: Verify and commit**

Run: `cd backend && python -m pytest tests/service/test_checkpoint_service.py -q -m "not integration"`

Expected: all non-integration tests pass.

Commit:

```bash
git add backend/migrations/20260719_unique_research_checkpoint_session_id.sql backend/app/models/research.py backend/app/service/checkpoint_service.py backend/tests/service/test_checkpoint_service.py
git commit -m "feat: approve research outlines atomically"
```

### Task 4: Replace unconditional orchestration with a phase dispatcher

**Files:**
- Create: `backend/tests/service/deep_research_v2/test_phase_dispatcher.py`
- Modify: `backend/app/service/deep_research_v2/graph.py`

- [ ] **Step 1: Write resume regression tests**

Build fake agents that record calls. Parametrize persisted phases:

```text
test_resume_starts_at_persisted_phase[planning]
  called agents begin with architect then scout
test_resume_starts_at_persisted_phase[researching]
  called agents begin with scout and exclude architect
test_resume_starts_at_persisted_phase[analyzing]
  called agents begin with data_analyst then wizard and exclude architect/scout
test_resume_starts_at_persisted_phase[writing]
  called agents begin with writer and exclude architect/scout/data_analyst
test_resume_starts_at_persisted_phase[reviewing]
  called agents begin with critic and exclude prior-stage agents
test_init_pauses_after_outline_and_does_not_call_scout
  emitted event types end with checkpoint_saved then outline_pending_approval; scout call count == 0
test_waiting_approval_is_a_noop_for_dispatcher
  no agent called and no phase reset occurs
```

- [ ] **Step 2: Verify the current implementation fails**

Run: `cd backend && python -m pytest tests/service/deep_research_v2/test_phase_dispatcher.py -q`

Expected: FAIL because `_run_simplified()` resets every run to `init`.

- [ ] **Step 3: Implement ordered phase dispatch**

Extract one method per phase and drive execution from an ordered loop. Do not duplicate the full pipeline in approve/resume branches. After outline generation, save the paused checkpoint, emit exactly one `outline_pending_approval`, and return. At every other successful boundary, set the next phase before saving.

Preserve existing cancellation checks and UI checkpoint data. Ensure `save_checkpoint()` can persist `paused`, `running`, `failed`, or `completed` instead of forcing every save to `running`.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && python -m pytest tests/service/deep_research_v2/test_phase_dispatcher.py -q`

Expected: all tests pass.

Commit:

```bash
git add backend/app/service/deep_research_v2/graph.py backend/tests/service/deep_research_v2/test_phase_dispatcher.py
git commit -m "fix: resume deep research from persisted phases"
```

### Task 5: Add authenticated research and approval API contracts

**Files:**
- Create: `backend/tests/router/test_research_outline_approval.py`
- Modify: `backend/app/router/research_router.py`

- [ ] **Step 1: Write failing API tests**

Using FastAPI `TestClient`, dependency overrides, and a fake V2 service, cover:

```text
test_stream_requires_authentication
  POST /research/stream without token returns 401
test_stream_passes_current_user_id_to_v2
  fake V2 service receives str(current_user.id)
test_approve_returns_sse_for_owner
  status 200, content-type text/event-stream, first resumed phase is planning
test_approve_maps_missing_or_foreign_checkpoint_to_404
  both cases return identical 404 response body
test_approve_maps_stale_revision_to_409
  returns 409 and does not call V2 service
test_resume_rejects_awaiting_approval_with_409
  returns 409 with approval-required error code
test_cancel_read_and_delete_are_user_scoped
  another user's checkpoint always returns 404
```

- [ ] **Step 2: Verify failure**

Run: `cd backend && python -m pytest tests/router/test_research_outline_approval.py -q`

Expected: FAIL because research routes do not require a current user and the approve endpoint is absent.

- [ ] **Step 3: Define request models and authenticated endpoints**

Add Pydantic models with length and whitespace validation:

```python
class OutlineSectionRequest(BaseModel):
    id: str
    title: str
    description: str
    section_type: Literal["qualitative", "quantitative", "mixed"] = "mixed"
    requires_data: bool = False
    requires_chart: bool = False


class ResearchQuestionRequest(BaseModel):
    id: str
    text: str


class ApproveOutlineRequest(BaseModel):
    outline_revision: str
    sections: List[OutlineSectionRequest] = Field(min_length=3, max_length=12)
    research_questions: List[ResearchQuestionRequest] = Field(min_length=3, max_length=12)
```

Require `current_user: User = Depends(get_current_user_required)` on V2 stream, approve, resume, cancel, checkpoint read/list/delete routes. Pass `str(current_user.id)` into V2 research and checkpoint service calls.

Add `POST /research/outline/{session_id}/approve`, atomically approve before constructing `StreamingResponse`, then call `service_v2.research(query=info["query"], session_id=session_id, resume=True, user_id=str(current_user.id))` so the returned response continues from `planning`.

- [ ] **Step 4: Verify and commit**

Run: `cd backend && python -m pytest tests/router/test_research_outline_approval.py -q`

Expected: all tests pass.

Commit:

```bash
git add backend/app/router/research_router.py backend/tests/router/test_research_outline_approval.py
git commit -m "feat: add authenticated outline approval stream"
```

### Task 6: Establish frontend tests and reusable SSE consumption

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/features/deep-research/types.ts`
- Create: `frontend/src/features/deep-research/research-stream.test.ts`
- Create: `frontend/src/features/deep-research/research-stream.ts`

- [ ] **Step 1: Install test packages and scripts**

Run:

```bash
cd frontend
npm install --save-dev --legacy-peer-deps vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event @playwright/test
```

Add scripts:

```json
{
  "test": "vitest run",
  "test:watch": "vitest",
  "test:e2e": "playwright test"
}
```

- [ ] **Step 2: Write failing stream tests**

Test UTF-8 characters split across chunks, CRLF framing, multiple events per chunk, trailing buffer at EOF, `[DONE]`, malformed JSON routed to `onProtocolError`, and reader failure routed to `onConnectionError`.

The public API must be:

```typescript
export async function consumeResearchStream(
  stream: ReadableStream<Uint8Array>,
  handlers: {
    onEvent: (event: ResearchEvent) => void
    onProtocolError: (error: Error, raw: string) => void
    onConnectionError: (error: Error) => void
  },
): Promise<void>
```

- [ ] **Step 3: Run tests and verify failure**

Run: `cd frontend && npm test -- research-stream.test.ts`

Expected: FAIL because `consumeResearchStream` does not exist.

- [ ] **Step 4: Implement typed contracts and stream parser**

Define discriminated event types including `outline_pending_approval`, `planning_error`, and existing generic research events. Implement buffered `TextDecoder.decode(value, { stream: true })`, split frames on blank lines, concatenate all `data:` lines, and parse JSON once per frame.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- research-stream.test.ts`

Expected: all tests pass.

Commit:

```bash
git add frontend/package.json frontend/package-lock.json frontend/vitest.config.ts frontend/src/test/setup.ts frontend/src/features/deep-research/types.ts frontend/src/features/deep-research/research-stream.ts frontend/src/features/deep-research/research-stream.test.ts
git commit -m "test: add reusable research stream consumer"
```

### Task 7: Add revision-scoped local drafts and approval panel

**Files:**
- Create: `frontend/src/features/deep-research/outline-draft.ts`
- Create: `frontend/src/features/deep-research/outline-draft.test.ts`
- Create: `frontend/src/features/deep-research/OutlineApprovalPanel.tsx`
- Create: `frontend/src/features/deep-research/OutlineApprovalPanel.test.tsx`

- [ ] **Step 1: Write failing draft and component tests**

Cover revision-scoped restore, stale revision rejection, clear-after-approve, 800 ms debounced writes, chapter and question add/edit/delete/reorder, 3–12 limits, blank text, disabled approving state, and displaying 400/409 errors.

Use this component contract:

```typescript
interface OutlineApprovalPanelProps {
  sessionId: string
  outlineRevision: string
  initialSections: OutlineSection[]
  initialResearchQuestions: ResearchQuestion[]
  approving: boolean
  error?: string
  onApprove(plan: EditableResearchPlan): Promise<void>
}
```

- [ ] **Step 2: Verify failure**

Run: `cd frontend && npm test -- outline-draft.test.ts OutlineApprovalPanel.test.tsx`

Expected: FAIL because the helpers and component are absent.

- [ ] **Step 3: Implement local draft helpers**

Use key `deep-research-outline-draft:${sessionId}:${outlineRevision}`. Store `{ outlineRevision, sections, researchQuestions, savedAt }`. Validate parsed JSON before returning it; malformed storage returns `null` and is removed.

- [ ] **Step 4: Implement the Ant Design panel**

Use controlled local state, stable IDs created with `crypto.randomUUID()`, inline Inputs/TextAreas, add/delete controls, and Up/Down ordering controls. Show `Form.Item` validation messages and disable approval until both collections have 3–12 nonblank items.

- [ ] **Step 5: Verify and commit**

Run: `cd frontend && npm test -- outline-draft.test.ts OutlineApprovalPanel.test.tsx`

Expected: all tests pass.

Commit:

```bash
git add frontend/src/features/deep-research/outline-draft.ts frontend/src/features/deep-research/outline-draft.test.ts frontend/src/features/deep-research/OutlineApprovalPanel.tsx frontend/src/features/deep-research/OutlineApprovalPanel.test.tsx
git commit -m "feat: add editable research outline approval panel"
```

### Task 8: Integrate approval APIs and shared streaming into chat

**Files:**
- Modify: `frontend/src/api/session.ts`
- Modify: `frontend/src/pages/chat/index.tsx`
- Create: `frontend/src/pages/chat/deep-research-integration.test.tsx`

- [ ] **Step 1: Write a failing integration test**

Mock `deepsearch()` and `approveResearchOutline()` streams. Assert that `outline_pending_approval` renders the panel, approved edits are sent with the original revision, the same stream consumer handles approval events, success clears the panel/draft, `409` preserves edits, and rapid clicks issue one request.

- [ ] **Step 2: Verify failure**

Run: `cd frontend && npm test -- deep-research-integration.test.tsx`

Expected: FAIL because the approval API and panel integration are absent.

- [ ] **Step 3: Add streaming API methods**

Add typed `approveResearchOutline(sessionId, plan)` and `resumeResearch(sessionId)` functions using the same Axios fetch adapter, `Accept: text/event-stream`, and `responseType: 'stream'` as `deepsearch()`.

- [ ] **Step 4: Replace the nested parser and connect the panel**

Move byte/SSE parsing out of `sendChat()` and call `consumeResearchStream()` for initial, approve, and resume responses. Keep the existing event-to-chat-state behavior in one `handleResearchEvent` callback. On `outline_pending_approval`, store the plan and stop loading. On approve, call the new API and consume its stream; clear local draft only after the request is accepted.

- [ ] **Step 5: Verify targeted tests and lint**

Run:

```bash
cd frontend
npm test -- deep-research-integration.test.tsx research-stream.test.ts OutlineApprovalPanel.test.tsx
npx eslint src/features/deep-research src/api/session.ts src/pages/chat/index.tsx
```

Expected: tests pass; targeted ESLint introduces no new errors. Existing errors on untouched lines in `index.tsx` must be documented if the linter cannot scope by changed line.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/session.ts frontend/src/pages/chat/index.tsx frontend/src/pages/chat/deep-research-integration.test.tsx
git commit -m "feat: connect outline approval to deep research chat"
```

### Task 9: Add live Prompt evaluation and Playwright journey

**Files:**
- Create: `backend/tests/evals/test_outline_planning_eval.py`
- Create: `backend/tests/evals/cases/outline_planning_cases.json`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/deep-research-outline-approval.spec.ts`

- [ ] **Step 1: Add the 12-case Prompt evaluation corpus**

Include market, policy, technology, competition, supply-chain, international comparison, investment-risk, and company-strategy topics. The live eval must assert 100% schema/count/ID validity and at least 90% of sections receive two nonblank, nonduplicate queries. Mark it `llm_eval` and skip with a clear reason when credentials are unavailable.

- [ ] **Step 2: Run the eval parser without live credentials**

Run: `cd backend && python -m pytest tests/evals/test_outline_planning_eval.py -q -m llm_eval`

Expected: SKIP when credentials are absent, or PASS against configured DashScope.

- [ ] **Step 3: Add Playwright configuration and mocked journey**

Configure Chromium and a Vite web server. Route/mock the auth/session/research endpoints so the test deterministically covers:

```text
login → start research → receive outline → edit 3+ sections/questions
→ approve → receive keyword/search events → disconnect → resume
→ assert outline generation was not repeated
```

Add a separate case for stale revision returning `409` and preserving the local draft.

- [ ] **Step 4: Install browser and run E2E**

Run:

```bash
cd frontend
npx playwright install chromium
npm run test:e2e -- deep-research-outline-approval.spec.ts
```

Expected: all outline approval E2E tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/evals frontend/playwright.config.ts frontend/e2e/deep-research-outline-approval.spec.ts
git commit -m "test: cover outline approval quality and user journey"
```

### Task 10: Final migration, regression, and build verification

**Files:**
- Modify only files required by failures discovered in this task.

- [ ] **Step 1: Run backend fast tests with coverage**

Run:

```bash
cd backend
python -m pytest tests/service tests/router -q --cov=app/service/deep_research_v2 --cov=app/service/checkpoint_service.py --cov=app/router/research_router.py --cov-report=term-missing
```

Expected: all tests pass; every new branch in Architect, checkpoint approval, phase dispatch, and router error mapping is covered.

- [ ] **Step 2: Run PostgreSQL integration tests**

With `TEST_DATABASE_URL` pointed to an isolated PostgreSQL database:

```bash
cd backend
python -m pytest -m integration -q
```

Expected: concurrent approval has exactly one winner and the unique constraint rejects duplicate sessions.

- [ ] **Step 3: Run frontend unit and E2E tests**

Run:

```bash
cd frontend
npm test
npm run test:e2e -- deep-research-outline-approval.spec.ts
```

Expected: all tests pass.

- [ ] **Step 4: Run compile, targeted lint, and production build**

Run:

```bash
cd backend
python -m compileall -q app
cd ../frontend
npx eslint src/features/deep-research src/api/session.ts src/pages/chat/index.tsx
npm run build
```

Expected: backend compilation and production build pass. Targeted lint has no new errors; pre-existing full-repository lint debt remains outside scope.

- [ ] **Step 5: Verify migration behavior**

Run the duplicate preflight against a disposable database containing both a clean dataset and a deliberate duplicate. Expected: clean data creates the unique index; duplicate data aborts without deleting rows.

- [ ] **Step 6: Review the final diff and commit fixes**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -10
```

Expected: no whitespace errors, only intended files changed, and each logical task has a focused commit.

Commit any verification-only fixes with:

```bash
git add backend/app backend/tests backend/migrations frontend/src frontend/e2e frontend/package.json frontend/package-lock.json frontend/playwright.config.ts frontend/vitest.config.ts
git commit -m "fix: harden outline approval verification"
```
