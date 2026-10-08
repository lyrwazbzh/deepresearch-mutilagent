# DeepResearch-mutilagent：执行图合同

## 0. 定位

LangGraph StateGraph 驱动 `ResearchState`。Graph 负责路由、并行研究、合流、分析决策、结构覆盖检查、语义审核、数据库写路径和报告校验；ResearchTask 的创建、调度与生命周期终态由 Runtime / TaskService 管理。

```text
用户请求
  → TaskService 创建 ResearchTask(status=pending)
  → Query Completion
  → 初始化 ResearchState + checkpoint₀ + scheduling Outbox
  → Scheduler claim
  → ResearchTask.status=running
  → LangGraph StateGraph
  → START → Route
  → NodeRunner
  → StateManager.commit_node
  → Checkpoint + Outbox
  → EventBus + SSE
```

```text
Agent   → 语义工作
Graph   → 确定性控制流
Harness → Context、执行策略与恢复
Tool    → 原子执行能力
State   → 节点间结构化交接
```

## 1. 节点

| 节点 | 类型 | 绑定 | 主要产出 |
|---|---|---|---|
| Route | 控制 | RouteNode | route |
| Direct | 专家 | ChiefArchitect | assistant Message |
| Focused | 专家 | route.target_agent | assistant Message |
| Plan | 专家 | ChiefArchitect | plan |
| Retrieval | 控制 | Graph | Web / RAG / SQL 调度 |
| Web | 分支 | DeepScout | research_rounds[d].web |
| RAG | 分支 | DeepScout | research_rounds[d].rag |
| SQL | 分支 | DataAnalyst | research_rounds[d].sql、DatasetRef? |
| MergeEvidence | 控制 | 程序 | evidence_pool |
| AnalysisDecision | 控制 | 结构化决策 | analysis_requests |
| Compute | 分析 | CodeWizard | research_rounds[d].compute |
| Coverage | 控制 | 程序 | coverage |
| Deepen | 专家 | ChiefArchitect | plan、execution.depth |
| Critic | 专家 | CriticMaster | critic |
| DatabaseWriteGate | 控制 | Graph | 数据库写路径选择 |
| PrepareWrite | 条件节点 | DataAnalyst | pending_write |
| LeadWriter | 专家 | LeadWriter | ReportAst candidate |
| ValidateReport | 控制 | 程序 | Schema/Citation issues、report.draft |
| Revise | 专家 | LeadWriter | 新 ReportAst candidate |
| Finalize | 控制 | 程序 | report.final |

数据库写执行由 HITL 后的 WriteExecutor 完成，并从 checkpoint 返回主链。

## 2. 节点能力

| 节点 | 能力 |
|---|---|
| Route | 轻量模型结构化分类；无业务 Tool |
| Direct | ChiefArchitect 无 Tool 回答 |
| Focused | 目标执行 Agent 的有界 ReAct 与 Tool Policy |
| Plan / Deepen | ChiefArchitect 结构化 Hypothesis Plan、MemoryService recall |
| Web | search_web、read_webpage、render_page |
| RAG | search_documents、search_chunks、read_document |
| SQL | inspect_schema、sample_table、execute_readonly_sql、inspect_dataset、profile_dataset、query_dataset |
| AnalysisDecision | Evidence 合流后的结构化计算需求判断；无业务 Tool |
| Compute | sandbox_execute、generate_chart、render_table |
| Coverage | 结构完整性规则 |
| Critic | CriticResult structured output；无 Tool |
| PrepareWrite | WritePlan / Preflight 内部服务 |
| LeadWriter / Revise | ReportAst 生成 |
| ValidateReport | Report Schema + CitationValidation |
| MergeEvidence / DatabaseWriteGate / Finalize | 确定性控制逻辑 |

## 3. 主流程

```text
START
  → Route
       ├─ direct  → Direct → assistant Message → GraphSuccess
       ├─ focused → Focused(route.target_agent) → assistant Message → GraphSuccess
       └─ deep    → Plan
                      → Retrieval（Web / RAG / SQL）
                      → MergeEvidence
                      → AnalysisDecision
                           ├─ analysis_requests=[] → Coverage
                           └─ 非空 → Compute → MergeEvidence → Coverage
                      Coverage
                           ├─ incomplete + budget → Deepen → Retrieval
                           ├─ incomplete + stop condition → GraphFailure
                           └─ complete → Critic
                      Critic
                           ├─ deepen + budget   → Deepen → Retrieval → MergeEvidence
                           ├─ retrieve + budget → Retrieval → MergeEvidence
                           ├─ gap + stop condition → GraphFailure
                           └─ pass + score>=8 → DatabaseWriteGate
                      → LeadWriter
                      → ValidateReport
                           ├─ invalid + revision budget → Revise → ValidateReport
                           ├─ invalid + exhausted → GraphFailure
                           └─ valid → Finalize
```

`GraphSuccess / GraphFailure` 是 Graph 返回给 Runtime 的运行结果，不是 ResearchState patch。Graph 正常结束后由 `TaskService.mark_completed(task_id)` 更新 ResearchTask；未处理异常或研究停止条件由 `TaskService.mark_failed(task_id, reason)` 收敛终态。

Critic 的 `deepen` 与 `retrieve` 在完成新一轮 Retrieval 后都重新进入 `MergeEvidence → AnalysisDecision → Coverage → Critic`，不会绕过合流、计算判断或结构检查。

## 4. 研究执行

### 4.1 Retrieval

当前轮查询来自 `plan.hypotheses[].queries[]`，或 Critic 的 `retrieve` 缺口：

```text
channel=web → Web
channel=rag → RAG
channel=sql → SQL
```

Graph 为实际运行的分支创建 NodeRun。各分支只提交自身的 `research_rounds[depth].{web|rag|sql}`，全部到达终态后进入 MergeEvidence。

SearchHit、SQL 原始响应和 RAG Candidate 是节点内的中间结果，不写入 branch items。执行 Agent 只把已经完成内容读取、定位、相关性与 stance 判断的 adopted results 提交到所属 branch；MergeEvidence 再为每个 Hypothesis 生成独立 Evidence 卡片。不同来源不因 `content_hash` 相同而合并；同一逻辑 ToolResult replay 复用原 Evidence。

Branch Result 的准入、归一化、identity、去重和原子提交规则统一见 [`evidence_pipeline.md`](evidence_pipeline.md)。

### 4.2 AnalysisDecision 与 Compute

MergeEvidence 后统一判断是否需要进一步计算：

```text
query + hypotheses + Evidence + DatasetRef + remaining_budget
  → AnalysisDecision
  → analysis_requests[]
```

AnalysisDecision 是 `analysis_requests` 的唯一写入节点。DataAnalyst 不能在 SQL 分支直接生成 AnalysisRequest。

```text
AnalysisRequest
  → CodeWizard
  → Workspace Artifact + Compute Manifest
  → research_rounds[depth].compute
  → MergeEvidence
  → Derived Evidence(derived_from + execution_ref)
```

Compute 不属于 Retrieval；Critic 的 `retrieve.target_channel` 只允许 `web | rag | sql`。

Graph 可以在 Evidence 提交后非阻塞调度 Task KG 派生更新；构图不阻塞 Coverage、Critic 或 Writer。

### 4.3 Coverage

Coverage 是确定性结构检查：

```text
Hypothesis 是否有 Evidence
Query / channel / branch 是否达到终态
AnalysisRequest 是否完成或明确不需要
```

Coverage 不判断 Evidence 是否足以证明 Hypothesis。结构完整才进入 Critic；结构缺口且仍有 depth/TaskBudget 时进入 Deepen。达到 depth/TaskBudget 上限或没有新 Query 时写入 `coverage.stop_reason`，Graph 以研究未完成结果结束，再由 TaskService 标记 ResearchTask 失败，不生成报告。

### 4.4 Critic

CriticMaster 是 Writer 前唯一的语义审核 Gate；补证或 Deepen 返回后会再次经过该 Gate，Writer 后不再执行：

```text
query + hypotheses + coverage + Evidence
  → score
  → pass | deepen | retrieve
```

```text
pass + score>=8 → DatabaseWriteGate
deepen          → ChiefArchitect / Deepen
retrieve        → Web / RAG / SQL
```

Critic 发现计算缺口时使用 deepen；Chief 更新研究计划后，新的计算需求仍由后续 AnalysisDecision 产生。

Critic 要求补证但 depth/TaskBudget 已耗尽时写入 `reliability.errors`，Graph 以研究未完成结果结束，再由 TaskService 标记 ResearchTask 失败；只有 `pass + score>=8` 能进入 Writer。

## 5. 数据库写路径

`plan.write_intent` 表示用户明确提出的数据库写入目标。只有研究通过 Critic 后才进入写路径：

```text
DatabaseWriteGate
  ├─ write_intent 有值
  │    → PrepareWrite
  │    → PendingWrite + checkpoint
  │    → TaskService.mark_paused(task_id)
  │    → HITL
  │    → WriteExecutor
  │    → LeadWriter
  └─ write_intent 为空
       → LeadWriter
```

## 6. Writer、验证与 Finalize

```text
LeadWriter
  → ReportAst candidate
  → Report Schema Validation
  → CitationValidation
       ├─ invalid → Revise（有界）→ 重新验证
       └─ valid → 发布 report.draft
  → Finalize → report.final = report.draft
```

Writer 后不再运行 Critic。Schema/Citation 校验是确定性的；校验修订预算耗尽时 Graph 返回失败结果，不提升无效 Draft。

Finalize 只提升 `report.final`。校验修订预算耗尽时 Graph 返回失败结果，ResearchTask 终态仍由 TaskService 更新。

## 7. Replan

```text
POST /tasks/{task_id}/replan
  → 保持当前 ResearchTask
  → 用户 instruction 写入 conversation_messages
  → 同 Thread 有界近期消息补全完整 query
  → MemoryService 重新召回 memory_context
  → RouteNode 重算 direct / focused / deep
  → deep 时 ChiefArchitect 生成新 Plan
  → direct/focused 时清空当前 active plan
  → 按新 route 继续
```

既有 `research_rounds`、`evidence_pool` 与 checkpoint 作为历史研究资产保留；当前节点只选择新 Plan Hypothesis IDs 绑定的 Evidence。Task KG 在同一 `tenant_id + task_id` namespace 中按新问题异步重建。

普通新用户请求通过 `POST /tasks` 创建新的 ResearchTask；系统驱动的 Deepen 与运行中 Replan 均保留在当前 Task 内。

## 8. NodeRunner 与恢复

`node_run_id` 标识一次语义节点执行，并在 retry 与 checkpoint replay 中稳定。每次真实 handler 调用只递增 Runtime 的 `node_attempt_no`。

```text
NodeRunner.begin
  → 按 node_key + input_checkpoint_id 创建或复用 NodeRun
  → 注入 deadline、cancellation_token、Context Packet 与能力集合
  → handler 返回字段级 patch
  → StateManager.commit_node
       行锁 + lease fencing + 字段所有权
       NodeRun + latest_state + checkpoint + Event + Outbox 同事务提交
```

副作用键：

```text
operation_key = sha256(tenant_id|task_id|node_run_id|effect_name)
```

恢复读取最近成功 checkpoint 和 NodeRun；已完成节点直接回放结果，未完成 graph position 重新调度。

恢复时四类对象各自回答一个问题：

```text
ResearchTask  → 当前任务是否仍应调度
Checkpoint    → Graph 从哪个位置继续
ResearchState → 恢复哪些研究业务数据
NodeRun       → 哪些语义节点已经完成并可复用
```

Outbox 只负责可靠唤醒与事件发布，不形成第五套恢复状态。

## 9. 验收标准

```text
□ ResearchTask 创建、Query Completion、State 初始化和 Scheduler claim 位于 Graph 外
□ StateGraph 主链从 START → Route 开始，支持 direct / focused / deep
□ RouteNode 与 ChiefArchitect 均不调用业务 Tool
□ Retrieval 只包含 Web / RAG / SQL
□ AnalysisDecision 是 AnalysisRequest 的唯一生产者
□ Compute 在 Evidence 合流后执行，不属于 Critic retrieve channel
□ Coverage 只做结构检查，Critic 只做语义审核
□ Critic 位于 Writer 之前，Writer 后只有 Schema/Citation Validation
□ score<8 或研究缺口回到 Deepen / Retrieval，不先生成报告
□ report.final 只指向确定性校验通过的 report.draft
□ NodeRun、Checkpoint、Outbox 与 lease fencing 保持原恢复合同
□ Graph Node 不写 ResearchTask.status；任务终态由 TaskService / Runtime 收敛
```
