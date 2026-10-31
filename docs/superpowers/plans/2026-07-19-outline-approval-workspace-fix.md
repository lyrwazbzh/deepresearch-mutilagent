# Outline Approval Workspace Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除大纲审批的 404，并将待审批大纲放入右侧宽工作区，使聊天与编辑界面在宽屏和窄屏下都可用。

**Architecture:** 后端在检查点服务边界递归清理运行时状态，并在持久化失败时阻止“可审批”事件。前端由聊天页统一决定右侧区域显示审批工作区还是研究结果，审批组件只负责宽表单编辑。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/JSONB, pytest, React 19, TypeScript, Ant Design 5, SCSS Modules, Vitest, Testing Library, Playwright.

---

### Task 1: Make checkpoint state JSON-safe

**Files:**
- Modify: `backend/tests/service/test_checkpoint_service.py`
- Modify: `backend/app/service/checkpoint_service.py`

- [ ] **Step 1: Write the failing serializer regression test**

Add a test that passes `_message_queue: asyncio.Queue()`, nested UUID/datetime values, and normal outline data to `_clean_state_for_storage`. Assert that runtime keys are absent and `json.dumps(cleaned)` succeeds.

```python
def test_clean_state_drops_runtime_objects_and_produces_json():
    state = {
        "query": "产业研究",
        "_message_queue": asyncio.Queue(),
        "nested": {"owner": UUID(USER_ID)},
    }
    cleaned = CheckpointService()._clean_state_for_storage(state)
    assert "_message_queue" not in cleaned
    assert cleaned["nested"]["owner"] == USER_ID
    json.dumps(cleaned)
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/service/test_checkpoint_service.py::test_clean_state_drops_runtime_objects_and_produces_json -q`

Expected: FAIL because `_message_queue` is still present and the returned dictionary is not JSON-serializable.

- [ ] **Step 3: Implement recursive cleaning**

Change `_clean_state_for_storage` to drop underscore-prefixed keys, recursively clean dictionaries/lists/tuples, preserve primitives, convert UUID/datetime to strings, discard unsupported objects, and finally run `json.dumps(cleaned)` without `default=str`.

```python
def _clean_state_for_storage(self, state: Dict[str, Any]) -> Dict[str, Any]:
    unsupported = object()

    def clean_value(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if str(key).startswith("_"):
                    continue
                cleaned = clean_value(item)
                if cleaned is not unsupported:
                    result[str(key)] = cleaned
            return result
        if isinstance(value, (list, tuple)):
            return [cleaned for item in value if (cleaned := clean_value(item)) is not unsupported]
        return unsupported

    clean = clean_value(state)
    json.dumps(clean)
    return clean
```

- [ ] **Step 4: Run checkpoint service tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/service/test_checkpoint_service.py -q`

Expected: all checkpoint service tests PASS.

- [ ] **Step 5: Commit the serializer fix**

```powershell
git add backend/app/service/checkpoint_service.py backend/tests/service/test_checkpoint_service.py
git commit -m "fix: persist deep research checkpoints safely"
```

### Task 2: Never expose an approval action without a durable checkpoint

**Files:**
- Create: `backend/tests/service/test_graph_outline_checkpoint.py`
- Modify: `backend/app/service/deep_research_v2/graph.py`

- [ ] **Step 1: Write the failing graph regression test**

Construct a minimal `DeepResearchGraph` with a fake architect that moves state to `awaiting_outline_approval` and a checkpoint service that returns failure. Consume `_run_simplified` and assert an error event is emitted while `outline_pending_approval` is absent.

```python
@pytest.mark.asyncio
async def test_failed_checkpoint_never_emits_outline_approval(monkeypatch):
    graph = object.__new__(DeepResearchGraph)
    graph.architect = FakeArchitect()
    graph.checkpoint_service = FailingCheckpointService()
    monkeypatch.setattr(graph_module, "clear_cancel_flag", lambda _session_id: None)
    monkeypatch.setattr(graph_module, "is_research_cancelled", lambda _session_id: False)

    events = [event async for event in graph._run_simplified(initial_state())]

    assert any(event["type"] == "error" for event in events)
    assert not any(event["type"] == "outline_pending_approval" for event in events)
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/service/test_graph_outline_checkpoint.py -q`

Expected: FAIL because the graph currently emits `outline_pending_approval` even when `cp_event` is `None`.

- [ ] **Step 3: Stop the stream on persistence failure**

When `save_checkpoint_async(..., status="paused")` returns `None`, yield:

```python
{
    "type": "error",
    "content": "研究大纲保存失败，请重新发起深度研究。",
}
```

Then return before emitting `outline_pending_approval`.

- [ ] **Step 4: Run the graph regression test and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/service/test_graph_outline_checkpoint.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the graph guard**

```powershell
git add backend/app/service/deep_research_v2/graph.py backend/tests/service/test_graph_outline_checkpoint.py
git commit -m "fix: require durable outline checkpoints"
```

### Task 3: Route outline approval into the right workspace

**Files:**
- Modify: `frontend/src/pages/chat/deep-research-integration.test.tsx`
- Modify: `frontend/src/pages/chat/index.tsx`
- Modify: `frontend/src/api/session.ts`

- [ ] **Step 1: Write the failing layout integration assertion**

Update the `ComPageLayout` test double to expose `main` and `right` regions. After receiving an outline event, assert the approval heading is under `right-workspace`, not under `chat-main`, and approval requests use `errorToast: false`.

```tsx
default: ({ children, sender, right }) => (
  <main>
    <section data-testid="chat-main">{children}{sender}</section>
    <aside data-testid="right-workspace">{right}</aside>
  </main>
)

expect(within(screen.getByTestId('right-workspace')).getByText('审核研究大纲')).toBeVisible()
expect(within(screen.getByTestId('chat-main')).queryByText('审核研究大纲')).toBeNull()
```

- [ ] **Step 2: Run the test and verify RED**

Run: `npm test -- src/pages/chat/deep-research-integration.test.tsx`

Expected: FAIL because the panel is rendered inside the chat main area.

- [ ] **Step 3: Implement workspace routing**

Set `rightPanelContent` to `OutlineApprovalPanel` while `pendingOutline` exists; otherwise preserve the existing research detail logic. Remove the panel from `.chat-page`. Set `errorToast: false` in `researchStreamConfig` so the inline recovery message is the only error UI.

```tsx
if (pendingOutline) {
  return <OutlineApprovalPanel {...outlineApprovalProps} />
}
if (isDeepResearchMode) {
  return <ResearchDetail {...researchDetailProps} />
}
```

```ts
function researchStreamConfig(options?: AxiosRequestConfig): AxiosRequestConfig {
  return {
    headers: { Accept: 'text/event-stream' },
    responseType: 'stream',
    adapter: 'fetch',
    loading: false,
    errorToast: false,
    ...options,
  }
}
```

- [ ] **Step 4: Run the integration test and verify GREEN**

Run: `npm test -- src/pages/chat/deep-research-integration.test.tsx`

Expected: all integration tests PASS.

- [ ] **Step 5: Commit workspace routing**

```powershell
git add frontend/src/pages/chat/index.tsx frontend/src/pages/chat/deep-research-integration.test.tsx frontend/src/api/session.ts
git commit -m "fix: move outline approval to research workspace"
```

### Task 4: Build the responsive Chinese approval workspace

**Files:**
- Modify: `frontend/src/features/deep-research/OutlineApprovalPanel.test.tsx`
- Modify: `frontend/src/features/deep-research/OutlineApprovalPanel.tsx`
- Create: `frontend/src/features/deep-research/OutlineApprovalPanel.module.scss`
- Modify: `frontend/src/components/page-layout/index.scss`

- [ ] **Step 1: Write failing Chinese UI and structure tests**

Assert the component exposes `审核研究大纲`, `确认大纲并开始研究`, Chinese accessible labels, and a friendly missing-checkpoint message instead of a bare UUID.

```tsx
expect(screen.getByRole('heading', { name: '审核研究大纲' })).toBeVisible()
expect(screen.getByRole('button', { name: '确认大纲并开始研究' })).toBeEnabled()
expect(screen.getByText('未找到可审核的研究大纲，请重新发起深度研究。')).toBeVisible()
```

- [ ] **Step 2: Run the component test and verify RED**

Run: `npm test -- src/features/deep-research/OutlineApprovalPanel.test.tsx`

Expected: FAIL because the current component uses English copy and has no workspace structure.

- [ ] **Step 3: Implement the workspace UI**

Replace inline styles with SCSS module classes. Use a flex-column workspace with a compact header, scrollable content, max-width form column, chapter cards, wrapping action rows, and a sticky footer. Translate all visible and accessible copy to Chinese. Normalize UUID-only errors to `未找到可审核的研究大纲，请重新发起深度研究。`.

```tsx
<section className={styles.workspace} aria-label="研究大纲审核工作区">
  <header className={styles.header}>
    <Title level={3}>审核研究大纲</Title>
    <Paragraph>调整章节和研究问题，确认后开始深度研究。</Paragraph>
  </header>
  <div className={styles.scrollArea}>
    <div className={styles.content}>{sectionEditors}{questionEditors}</div>
  </div>
  <footer className={styles.footer}>
    <Button type="primary">确认大纲并开始研究</Button>
  </footer>
</section>
```

```scss
.workspace { height: 100%; min-width: 0; display: flex; flex-direction: column; }
.scrollArea { min-height: 0; flex: 1; overflow-y: auto; }
.content { width: min(100%, 960px); margin: 0 auto; padding: 24px 32px 40px; }
.footer { flex: none; padding: 16px 32px; border-top: 1px solid #edf0f5; background: #fff; }
```

- [ ] **Step 4: Add narrow-screen layout**

At widths below 900px, make `.wide-right-mode` stack the chat and right workspace vertically, remove fixed 35% width constraints, and keep form controls at full width with 44px touch targets.

```scss
@media (max-width: 900px) {
  .com-page-layout.wide-right-mode {
    height: auto;
    overflow: visible;
    flex-direction: column;
  }
  .com-page-layout.wide-right-mode .com-page-layout__main,
  .com-page-layout.wide-right-mode .com-page-layout__right {
    width: 100%;
    min-width: 0;
    max-width: none;
  }
}
```

- [ ] **Step 5: Run component and integration tests and verify GREEN**

Run: `npm test -- src/features/deep-research/OutlineApprovalPanel.test.tsx src/pages/chat/deep-research-integration.test.tsx`

Expected: all selected frontend tests PASS.

- [ ] **Step 6: Commit the responsive UI**

```powershell
git add frontend/src/features/deep-research/OutlineApprovalPanel.tsx frontend/src/features/deep-research/OutlineApprovalPanel.module.scss frontend/src/features/deep-research/OutlineApprovalPanel.test.tsx frontend/src/components/page-layout/index.scss
git commit -m "style: make outline approval responsive"
```

### Task 5: Verify the real user journey

**Files:**
- Modify: `frontend/e2e/deep-research-outline-approval.spec.ts`

- [ ] **Step 1: Update E2E selectors and layout assertions**

Use Chinese labels and assert the approval workspace bounding box is wider than the chat main region at desktop width. Add a 768px viewport assertion that controls remain visible without horizontal document overflow.

```ts
const workspace = page.getByLabel('研究大纲审核工作区')
await expect(workspace).toBeVisible()
expect((await workspace.boundingBox())!.width).toBeGreaterThan(600)
expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
```

- [ ] **Step 2: Run the focused E2E test**

Run: `npm run test:e2e -- deep-research-outline-approval.spec.ts --project=chromium`

Expected: both approval/resume and stale-revision journeys PASS.

- [ ] **Step 3: Run full verification**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests\service backend\tests\router\test_research_outline_approval.py -q
cd frontend
npm test
npm run build
npm run test:e2e -- deep-research-outline-approval.spec.ts --project=chromium
```

Expected: backend tests, frontend tests, production build, and focused browser tests all PASS.

- [ ] **Step 4: Restart local services and manually verify**

Restart the feature-worktree backend and frontend, confirm `/hello` and `/` return HTTP 200, then execute one real deep-research request through outline generation and approval without a 404.

- [ ] **Step 5: Commit E2E coverage**

```powershell
git add frontend/e2e/deep-research-outline-approval.spec.ts
git commit -m "test: cover responsive outline approval workspace"
```
