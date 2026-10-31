# 深度研究 V2：用户审核大纲设计

日期：2026-07-19
状态：已完成交互设计确认，待用户复核文档

## 1. 目标

深度研究 V2 在开始搜索前先生成研究大纲并暂停。用户可以审核和修改章节及核心研究问题，批准后系统基于最终计划生成关键词与研究假设，再继续搜索、分析、写作和审核。

首版必须解决以下问题：

- 用户能在昂贵的搜索开始前纠正研究方向。
- 用户修改后的章节和研究问题是后续关键词及假设生成的唯一计划输入。
- 暂停、批准、断线恢复和重复提交具有明确且可测试的状态语义。
- 研究任务只能由所属用户读取或修改。
- 首次研究流和批准后的续流使用同一套 SSE 解析逻辑。

## 2. 已确认的产品范围

首版交付完整垂直切片：

- 展示、编辑、新增、删除和排序章节。
- 展示、编辑、新增、删除和排序核心研究问题。
- 章节数量限制为 3–12，初始默认生成 5–8 章。
- 核心研究问题数量限制为 3–12，初始默认生成 3–6 个。
- 用户批准后重新生成关键词与研究假设。
- 浏览器本地自动保存未批准草稿。
- 支持权限校验、重复批准保护、阶段恢复、自动化测试和 Prompt 评测。

## 3. 状态模型

`phase` 表示当前需要执行的研究阶段，`status` 表示任务是否正在运行。

```text
新研究
  │
  ▼
init ──生成章节与研究问题──▶ awaiting_outline_approval
                                  │
                                  │ status=paused
                                  ▼
                         用户编辑并批准
                                  │
                                  │ 原子写入最终计划
                                  ▼
planning ──关键词与假设──▶ researching ──▶ analyzing
                                              │
                                              ▼
completed ◀── reviewing ◀── writing ◀─────────┘
```

状态规则：

- 新任务从 `phase=init, status=running` 开始。
- 大纲初稿生成成功后保存为 `phase=awaiting_outline_approval, status=paused`，然后结束当前 SSE。
- 等待审核状态只能通过大纲批准接口继续；通用 `/resume` 对该状态返回 `409 Conflict`。
- 批准事务成功后切换为 `phase=planning, status=running`。
- 每个阶段成功后，先将 `phase` 更新为下一阶段，再保存检查点。
- 阶段中途失败时保留当前 phase；恢复最多重跑当前阶段，不重跑已完成的前序阶段。
- 完成时保存 `phase=completed, status=completed`。

## 4. 数据契约

### 4.1 章节

```text
OutlineSection
  id: stable string
  title: non-empty string
  description: non-empty string
  section_type: qualitative | quantitative | mixed
  requires_data: boolean
  requires_chart: boolean
```

章节 ID 不与数组位置绑定。模型首次生成稳定 ID，用户新增章节由前端生成临时稳定 ID，服务端验证格式并在必要时规范化。排序、删除和新增不会改变已有章节 ID。

### 4.2 核心研究问题

```text
ResearchQuestion
  id: stable string
  text: non-empty string
```

研究问题是独立可排序列表，不要求用户维护问题到章节的人工关联。关键词生成同时接收最终章节与最终研究问题，由模型综合两者。

### 4.3 计划版本

大纲初稿生成时创建 `outline_revision`，与大纲一起保存在检查点并发送给前端。批准请求必须携带相同 revision。revision 不一致表示页面或本地草稿已过期，服务端返回 `409 Conflict`，不覆盖新状态。

### 4.4 关键词输出

关键词模型返回数组结构，不使用 `sec_N_queries` 一类位置字段：

```json
{
  "sections": [
    {
      "id": "section_stable_id",
      "queries": ["关键词一", "关键词二"]
    }
  ],
  "hypotheses": [
    {
      "id": "hypothesis_stable_id",
      "content": "可验证或证伪的研究假设"
    }
  ]
}
```

后端按稳定章节 ID 合并关键词。未知 ID、重复 ID、空关键词或非数组字段均计为无效输出。

## 5. API 设计

### 5.1 创建研究流

`POST /research/stream`

- 要求登录。
- 使用当前用户创建检查点并写入 `user_id`。
- 执行 `init` 阶段，生成章节与研究问题。
- 保存等待审核检查点。
- 发送一次 `outline_pending_approval` 事件后结束 SSE。

事件负载：

```json
{
  "type": "outline_pending_approval",
  "session_id": "session-id",
  "outline_revision": "revision-id",
  "sections": [],
  "research_questions": []
}
```

### 5.2 批准大纲并续流

`POST /research/outline/{session_id}/approve`

- 要求登录并按 `user_id + session_id` 查询检查点。
- 请求体包含完整章节、完整研究问题和 `outline_revision`。
- 在短事务中锁定唯一检查点记录。
- 验证所有权、`awaiting_outline_approval + paused`、revision、数量、稳定 ID 和文本字段。
- 保存最终计划并切换为 `planning + running` 后提交事务。
- 事务提交后开始 LLM 调用，并以同一 HTTP 响应返回后续 SSE。
- 重复批准、并发批准、错误阶段或过期 revision 返回 `409 Conflict`。

### 5.3 通用恢复

`POST /research/resume/{session_id}`

- 要求登录并校验所有权。
- 只恢复失败或断线任务。
- 等待大纲审核时返回 `409 Conflict`，不能绕过批准。
- 根据持久化 phase 从对应阶段继续。

### 5.4 权限和错误语义

- 检查点不存在和不属于当前用户统一返回 `404 Not Found`。
- Pydantic 请求结构错误返回 `422 Unprocessable Entity`。
- 合法 JSON 但违反业务约束返回 `400 Bad Request`。
- 状态、revision 或并发冲突返回 `409 Conflict`。
- 关键词规划失败通过 SSE `planning_error` 通知用户，并保留可恢复检查点。

研究检查点的读取、批准、恢复、取消和删除入口均执行同一所有权校验。

## 6. 后端组件边界

```text
research_router.py
  └─ 身份校验、请求校验、HTTP/SSE 映射

checkpoint_service.py
  └─ 唯一检查点读写、事务锁、原子批准

architect.py
  ├─ generate_outline()
  └─ generate_search_plan()

graph.py
  └─ phase 分发与阶段串联
```

### 6.1 Architect

`generate_outline()` 只生成章节和研究问题，不生成关键词或假设。成功后写入稳定 ID、revision 和等待审核 phase。

`generate_search_plan()` 读取用户批准后的章节与研究问题，生成按章节 ID 关联的关键词以及研究假设。

### 6.2 CheckpointService

新增原子批准操作，内部完成：

1. 按用户和 session 查询并锁定记录。
2. 校验 phase、status 和 revision。
3. 校验并规范化最终计划。
4. 覆盖检查点 state 中的章节及研究问题。
5. 设置 `planning + running`。
6. 提交并返回更新后的 state。

LLM 调用不在数据库事务内执行。

### 6.3 阶段分发器

现有 `_run_simplified()` 无条件重置为 `init`，无法正确恢复。实现改为统一阶段分发，不为大纲批准另建第二套流水线。

```text
dispatch(state.phase)
  init         → generate_outline → pause
  planning     → generate_search_plan
  researching  → scout
  analyzing    → data analyst + wizard
  writing      → writer
  reviewing    → critic/research/revise loop
  completed    → no-op
```

## 7. 关键词容错

关键词生成执行以下规则：

1. 校验整体 JSON、章节 ID、关键词数组和假设结构。
2. 首次失败后，将具体校验错误反馈给模型并自动重试一次。
3. 重试后若至少三个章节获得有效关键词，继续执行，并为所有缺失章节生成确定性兜底查询：
   - `原始研究问题 + 章节标题`
   - `章节标题 + 章节描述`
4. 有效章节少于三个时停止搜索，保持 `phase=planning`，将任务标记为可恢复失败并发送 `planning_error`。
5. 用户通过通用恢复入口重新尝试关键词生成。

使用兜底时发送可观测事件并记录缺失章节 ID，便于质量监控和 Prompt 迭代。

## 8. 前端设计

```text
researchStream.ts / useResearchStream
  └─ 字节解码、SSE 分帧、JSON 解析、研究事件分发

OutlineApprovalPanel
  ├─ 章节编辑、增删、排序
  ├─ 研究问题编辑、增删、排序
  ├─ 3–12 数量和非空校验
  └─ 批准、加载和错误状态

chat/index.tsx
  └─ 组合研究流、审核面板和现有聊天状态
```

首次 `/research/stream` 与批准接口返回的 SSE 必须使用同一研究流消费器，不复制 parser 或事件分发逻辑。

### 8.1 本地草稿

- localStorage key 包含 `session_id + outline_revision`。
- 修改后防抖保存章节和研究问题。
- 只恢复 revision 相同的草稿。
- 批准成功后清除草稿。
- revision 不同时保留旧数据但不自动套用，避免旧页面覆盖新计划。

### 8.2 用户交互

- 章节和问题均支持新增、删除、编辑和排序。
- 少于 3 或超过 12 项时禁止批准并显示就地错误。
- 标题、描述和问题文本为空时禁止批准。
- 批准请求进行时禁用编辑和按钮，防止重复提交。
- `409` 时重新加载服务端状态，不自动重试批准。
- SSE 断线时保留当前 UI，并提供恢复操作。

## 9. 数据库迁移

`research_checkpoints.session_id` 增加唯一约束。由于项目没有现成 Alembic 迁移体系，首版提供显式、可审查的 SQL 迁移：

1. 只读查询并列出所有重复 session ID。
2. 若存在重复记录则中止，不自动删除或覆盖研究数据。
3. 由人工确认保留记录并清理重复项。
4. 再次预检无重复后创建唯一索引。

应用层保存逻辑捕获唯一冲突，并重新读取已存在的检查点。批准事务仍使用行锁和状态条件，保证并发批准只有一个成功。

## 10. 测试设计

### 10.1 后端：pytest + pytest-asyncio

- 大纲正常生成、JSON 失败、重试、章节或问题数量不足。
- 批准成功、无权限、过期 revision、重复批准和并发批准。
- 章节与问题数量边界、空文本、重复 ID 和未知字段。
- 关键词全部成功、部分成功、至少三章有效、少于三章有效和重试失败。
- 从 `planning`、`researching`、`analyzing`、`writing`、`reviewing` 恢复，且不重复执行已完成阶段。
- 等待审核状态不能通过通用 resume 绕过。
- 唯一迁移预检能发现重复项并安全中止。

### 10.2 前端：Vitest + Testing Library

- SSE JSON 被拆成任意字节块时正确解析。
- 单个数据块包含多个 SSE 事件。
- 非法事件、残余 buffer、流结束和网络中断。
- 章节与问题的编辑、增删、排序和边界校验。
- localStorage 防抖保存、恢复、批准后清理和 revision 过期。
- 批准期间防重复点击，以及 400、404、409 和 SSE 错误展示。

### 10.3 端到端：Playwright

```text
登录
  → 发起深度研究
  → 收到大纲并暂停
  → 编辑章节和问题
  → 批准
  → 关键词生成
  → 搜索开始
  → 模拟断线
  → 恢复且不重复规划
```

### 10.4 Prompt 评测

Prompt 评测使用独立标记，默认快速测试不调用真实 LLM。评测集包含 12 个代表性行业研究问题，门槛为：

- 大纲结构合格率 100%。
- 章节和研究问题数量合格率 100%。
- 稳定 ID 及关键词映射合格率 100%。
- 有效关键词覆盖率不低于 90%。

## 11. 性能和可观测性

- 批准事务提交后才调用 LLM，避免长事务。
- 每次请求只加载一次完整检查点。
- localStorage 写入防抖。
- 不增加轮询、后台队列或常驻服务。
- 规划阶段增加一次 LLM 调用，预计增加约 3–5 秒和约 1500 tokens。
- 记录大纲生成重试次数、关键词生成重试次数、兜底章节数、批准冲突数和各阶段恢复次数。

## 12. 复用的现有能力

- `ResearchCheckpoint` 及 JSON 状态存储。
- `CheckpointService` 的保存、加载和状态更新能力。
- V2 SSE 流式响应。
- JWT 用户系统和后端用户依赖。
- 现有 Architect、Scout、Data Analyst、Wizard、Writer 和 Critic Agent。
- 取消标志、报告生成流程和前端聊天状态。

## 13. 明确不在首版范围

- 跨设备同步未批准草稿：首版只使用当前浏览器 localStorage。
- 匿名用户深度研究：首版要求登录和任务所有权。
- 研究执行过程中再次修改大纲：只允许执行前审核。
- 后台任务队列和独立进度订阅系统：继续使用请求内 SSE。
- 整个聊天页面的视觉重构：仅增加审核面板和必要状态。
- 将研究编排改造成通用工作流框架：只做阶段分发所需的针对性重构。
- 自动删除数据库历史重复检查点：迁移发现重复时必须人工确认。

## 14. 验收标准

功能完成必须同时满足：

1. 用户能编辑并批准 3–12 个章节和 3–12 个研究问题。
2. 搜索开始前一定存在用户批准的最终计划。
3. 关键词按稳定章节 ID 关联，并使用最终计划生成。
4. 重复批准、过期页面和无权限请求不能改变任务状态。
5. 任一研究阶段断线后可从当前阶段恢复，不重复已完成阶段。
6. 首次流和续流共用同一 SSE 消费器。
7. 迁移不会自动删除历史数据。
8. 后端、前端、端到端和 Prompt 评测达到本设计规定的覆盖范围与门槛。
