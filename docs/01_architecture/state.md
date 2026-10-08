# DeepResearch-mutilagent：ResearchState 合同

## 0. 定位

`ResearchState` 是 Research Graph 的结构化交接面和 checkpoint 快照。

```text
ResearchState → 结构化流程状态、短内容与稳定引用
Workspace     → 当前 Task 的长结果、Compute 文件与报告
Document      → 可跨 Task 检索的知识库
evidence_pool → 按 Hypothesis 组织的统一证据入口
```

当前用户输入与同 Thread 近期上下文在入口补全为唯一的 `ResearchState.query`；用户原文保存在 `conversation_messages`。

## 1. 顶层结构

```text
ResearchState
  schema_version
  tenant_id
  user_id
  thread_id
  task_id

  query

  execution
  route?
  plan?
  memory_context?
  research_rounds{}
  evidence_pool[]
  analysis_requests[]
  coverage?
  critic?
  pending_write?
  report
  reliability
  timestamps
```

TaskBudget、lease、Node attempt 与 Context Packet 是 Harness/Runtime 数据，不进入 ResearchState。

### 1.1 ResearchTask 与 ResearchState

```text
ResearchTask
  → 一次可调度、可暂停、可取消、可恢复的执行实例
  → 保存身份、status、生命周期时间、lease 与 TaskBudget

ResearchState
  → 该执行实例在 LangGraph 中流动的研究业务状态
  → 不保存任务生命周期 status
```

`task_id` 只用于把 ResearchState 关联到当前 ResearchTask。ResearchTask 的持久化字段由 Runtime / Storage 合同定义；本文件只规定二者的所有权边界。

## 2. 字段合同

### 2.1 query

```text
当前用户输入 + 同 Thread 近期上下文
  → Query Completion
  → ResearchState.query
```

Query Completion 只读取当前消息之前、同一 Thread 的持久化 user/assistant 消息，并按固定消息数与 token budget 截取。任务创建和 Replan 覆盖 `query`；retry/replay 复用已经写入的值。

### 2.2 execution

```yaml
execution:
  depth: int
  max_depth: int
  revision_round: int
  max_revision_rounds: int
```

`depth` 限制研究深化轮次，`revision_round` 限制 Writer 对确定性报告校验 issue 的修订次数。NodeRun、attempt、lease 和调度状态保存在 Runtime 与 checkpoint。

### 2.3 memory_context

```yaml
memory_context:
  query_hash: string
  items:
    - memory_id: string
      revision: int
      content_hash: string
```

Plan / Replan / Deepen 前由 MemoryService 生成。ResearchState 只保存实际采用的记忆引用；正文按需读取并由 Context Builder 渲染。

### 2.4 route 与 plan

```yaml
route:
  mode: direct | focused | deep
  target_agent?: deep_scout | data_analyst | code_wizard

plan:
  hypotheses:
    - hypothesis_id: string
      claim: string
      evidence_needed: string[]
      refuted_if: string[]
      priority: int
      queries:
        - query_id: string
          channel: web | rag | sql
          text: string
  write_intent?: object
  deepen_history: object[]
```

`route` 由 RouteNode 写入。只有 `route.mode=deep` 时存在 `plan`；计算需求在 Evidence 合流后以 `analysis_requests` 表达。

### 2.5 research_rounds

```yaml
research_rounds:
  "{depth}":
    web: { status, items, error? }
    rag: { status, items, error? }
    sql: { status, items, error? }
    compute?: { status, items, error? }
```

分支状态为 `pending | completed | failed | skipped`。分支 `items[]` 只保存执行 Agent 已经采纳、完成内容读取、定位、相关性与 stance 判断的结果，以及其完整短内容或稳定引用和分页/截断状态。SearchHit、RAG Candidate、SQL 原始响应与 Compute 诊断保留在节点执行过程、ToolResult 或 Trace 中，不进入 branch items。

Branch Result 如何经过准入、归一化、稳定身份与 replay 规则成为 Evidence，以 [`evidence_pipeline.md`](evidence_pipeline.md) 为唯一事实源。

SQL 长结果可以同时返回 DatasetRef：

```yaml
dataset_ref:
  workspace_path: string
  content_hash: string
  schema_summary: object
```

DatasetRef 仍是 SQL/Data 内部交付引用，不形成顶层 Dataset 系统。

### 2.6 evidence_pool

```yaml
evidence_pool:
  - evidence_id: string
    hypothesis_id: string

    summary: string
    excerpt: string

    source_type: web | rag | sql | compute
    stance: support | refute | context
    relevance: float

    data?: object
    doc_id?: string
    chunk_id?: string
    workspace_path?: string

    source_uri?: string
    published_at?: datetime
    retrieved_at?: datetime

    content_hash: string

    derived_from?: string[]
    execution_ref?: string
```

字段职责：

```text
evidence_id   → 当前 Task 内的证据卡片身份
hypothesis_id → 该卡片正在验证的唯一 Hypothesis
summary       → 给 Agent 的有界说明
excerpt       → 实际用于支持/反驳判断的原文片段或结果片段
stance        → support | refute | context
relevance     → 与当前 Hypothesis 的相关性
content_hash  → 卡片指向完整内容的完整性校验
```

内容位置组合：

```text
Web 短结果 → data + source_uri + retrieved_at + content_hash
Web 长正文 → workspace_path + source_uri + retrieved_at + content_hash
RAG        → doc_id + chunk_id + content_hash
SQL 短结果 → data + content_hash
SQL 长结果 → workspace_path + content_hash
Compute    → workspace_path + derived_from + execution_ref + content_hash
```

`published_at` 是来源自身时间，`retrieved_at` 是本次读取时间。`data / doc_id+chunk_id / workspace_path` 三选一表达完整内容位置。

Evidence 的研究判断字段收敛为 `hypothesis_id + stance + relevance`；来源质量只参与 DeepScout 的候选筛选。

同一内容支持多个 Hypothesis 时，为每个 Hypothesis 创建独立 Evidence 卡片；它们可以共享相同来源引用和 `content_hash`。不同来源不因 hash 相同而合并。同一逻辑 ToolResult replay 复用相同 Hypothesis 下的原 `evidence_id`。

具体 identity、去重与 Merge 语义见 [`evidence_pipeline.md`](evidence_pipeline.md)；本节只定义正式 Evidence Schema。

`data` 保存完整规范化短结果及其 `truncated / has_more / next_cursor` 等完整性标记；超过 inline budget 时使用 Workspace。

### 2.7 analysis_requests

```yaml
analysis_requests:
  - analysis_id: string
    hypothesis_id: string
    objective: string
    input_evidence_ids: string[]
    expected_output: string
    status: pending | completed | failed
```

Post-Merge Analysis Decision 是唯一写入者。DataAnalyst、ChiefArchitect 和 CriticMaster 不生成或修改 AnalysisRequest。CodeWizard 只更新所执行请求的 `status`，并把输出写入 Compute branch；Derived Evidence 由 MergeEvidence 创建。

### 2.8 coverage

```yaml
coverage:
  complete: bool
  gaps:
    - hypothesis_id: string
      gap_type: missing_evidence | missing_channel | pending_query | pending_analysis
      channel?: web | rag | sql
      description: string
  stop_reason?: max_depth | budget_exhausted | no_new_queries
```

Coverage 只表达结构完整性，不评价 Evidence 的语义充分性。

### 2.9 critic

```yaml
critic:
  score: int
  decision: pass | deepen | retrieve
  gaps:
    - hypothesis_id: string
      reason: string
      target_channel?: web | rag | sql
```

CriticMaster 在 Writer 之前写入该字段。`pass` 仅在 `score >= 8` 时合法；`score < 8` 时 decision 为 `deepen | retrieve`。`retrieve.target_channel` 取 `web | rag | sql`；计算缺口使用 `deepen`，后续 AnalysisRequest 仍由 Post-Merge Analysis Decision 产生。

### 2.10 pending_write

用户明确提出数据库写入时，Plan 保存 `write_intent`。研究通过 Critic 后，PrepareWrite 创建 PendingWrite，并保存：

```text
pending_write = write_id
```

完整 WritePlan、Preview 与审批状态保存在 PendingWrite。

### 2.11 report

```yaml
report:
  draft?:
    version: int
    workspace_path: string
    content_hash: string
  final?:
    version: int
    workspace_path: string
    content_hash: string
```

LeadWriter / Revise 生成 ReportAst candidate。只有 Report Schema Validation 与 CitationValidation 均通过后才提交 `report.draft`；Finalize 执行 `report.final = report.draft`。Writer 后不再调用 Critic。

### 2.12 reliability

```yaml
reliability:
  errors: object[]
  fallbacks: object[]
  degraded_branches: object[]
```

只保存 Graph 需要读取的错误摘要和降级结果；attempt、CB、lease 与 checkpoint 细节留在 Runtime/Trace。

## 3. Patch、并发与字段所有权

```text
Node handler
  → validate patch schema
  → StateManager.commit_node
  → 行锁 + lease fencing + 字段所有权
  → NodeRun + latest_state + checkpoint + Event + Outbox 同事务提交
```

并行 Web / RAG / SQL 分支只写各自 branch；MergeEvidence 合流后，AnalysisDecision 才能写 `analysis_requests`。

| 写入者 | 写入字段 |
|---|---|
| Task creation / Replan intake | query |
| RouteNode | route；direct/focused 时清空当前 plan |
| Orchestrator / MemoryService | memory_context |
| ChiefArchitect / Plan | plan |
| ChiefArchitect / Deepen | plan.hypotheses、plan.deepen_history、execution.depth |
| DeepScout / Web | research_rounds[depth].web |
| DeepScout / RAG | research_rounds[depth].rag |
| DataAnalyst / SQL | research_rounds[depth].sql |
| MergeEvidence | evidence_pool |
| Post-Merge Analysis Decision | analysis_requests |
| CodeWizard / Compute | research_rounds[depth].compute、所执行 AnalysisRequest.status |
| Coverage | coverage |
| CriticMaster | critic |
| PrepareWrite / WriteExecutor | pending_write |
| LeadWriter / Revise | ReportAst candidate；验证通过后 report.draft |
| Finalize | report.final |
| Graph 控制节点 | execution、reliability、timestamps |

ResearchTask 的 `status`、claim、lease、pause、resume 与 cancel 只由 TaskService / Runtime 更新，不属于 ResearchState 字段所有权。

## 4. Context Projection

Harness 的 Context Builder 从 State 和引用生成 Per-Agent Context Packet：

```text
必要结构字段
+ 当前 Hypothesis / Query / AnalysisRequest
+ 必要 Evidence 卡片
    ├─ data → 直接读取完整短结果
    ├─ workspace_path → Workspace 范围读取
    └─ doc_id + chunk_id → Document 读取
+ remaining_budget 只读投影
```

任何 Agent 都不接收完整 ResearchState。上下文压缩只作用于派生摘要与历史 Observation，不覆盖 `query`、Evidence、原始 ToolResult、Document 或 Workspace 文件。

## 5. 恢复边界

每次成功 State commit 生成 checkpoint，记录 ResearchState snapshot、current graph position 与 LangGraph scheduling state。ResearchTask 决定任务是否仍应调度；Runtime 根据 NodeRun 复用已提交结果。

```text
ResearchTask  → 生命周期与是否继续调度
Checkpoint    → Graph 恢复位置与调度状态
ResearchState → 研究业务数据
NodeRun       → 已完成语义节点及其可复用结果
```

TaskBudget、Document Ingestion、ExportJob、Worker claim 和 ResearchTask 生命周期不进入 ResearchState。

## 6. SSE 投影

```text
task_updated.changed[]
  task_status | phase | route | messages | evidence |
  knowledge_graph | report | pending_write
```

`task_status` 来自 ResearchTask；`route / evidence / report / pending_write` 来自 ResearchState。SSE 可以组合两个来源形成 API/Event projection，但不把 `task_status` 写回 ResearchState。内部节点事件保留在 `research_events`，UI 只消费收敛后的资源变化通知。

## 7. 验收标准

```text
□ ResearchState.query 是补全后的唯一完整查询
□ RouteNode 独占 route；ChiefArchitect 不负责入口分类
□ 只有 deep 路径存在 Hypothesis Plan
□ 计算需求只由 Post-Merge Analysis Decision 写入 AnalysisRequest
□ 每项 Evidence 直接绑定一个 hypothesis_id
□ Evidence 只保留 stance、relevance、locator/content fields 与 content_hash
□ Compute Evidence 使用 derived_from + execution_ref
□ Coverage 只表达结构完整性，Critic 只表达语义审核
□ Critic 位于 Writer 之前，report.final 只来自确定性验证通过的 Draft
□ remaining_budget 只存在于 Context Packet，不进入 ResearchState
□ ResearchState 不保存 task_status；Task 生命周期只由 ResearchTask 表达
□ Finalize 只写 report.final，不修改 ResearchTask.status
□ NodeRun、checkpoint、Outbox 与 SSE 恢复合同保持分立
```
