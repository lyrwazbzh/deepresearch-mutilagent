# DeepResearch-mutilagent：Agent Harness / Runtime

## 0. 定位

Agent Harness 由现有执行组件共同组成，不是新的微服务或第二套状态系统：

```text
Agent Harness
├─ Context Management
├─ Execution Runtime
└─ State & Recovery
```

```text
Context Management
  → ResearchState / Evidence / Workspace / Document
  → Per-Agent Context Packet

Execution Runtime
  → Agent Loop、bounded ReAct、Tool Policy、deadline、TaskBudget、Model Binding、Sandbox、cancel

State & Recovery
  → Checkpoint、NodeRun、operation_key、Outbox、SSE replay、resume
```

Runtime 遵守唯一状态边界：

```text
ResearchTask
  → 运行生命周期与控制面
  → status / claim / lease / TaskBudget / pause / resume / cancel

ResearchState
  → LangGraph 研究业务状态
  → query / route / plan / evidence / coverage / critic / report
```

`task_status`、lease、attempt、TaskBudget 与 `remaining_budget` 都不属于 ResearchState。

Tool 错误和副作用属性由 [`tool.md`](../02_capabilities/tool.md) 定义；Graph 与字段所有权由 [`graph.md`](../01_architecture/graph.md) 和 [`state.md`](../01_architecture/state.md) 定义。

## 1. Context Management

### 1.1 Context Builder

Harness 不把完整 ResearchState 注入任何 Agent：

```text
ResearchState + 当前节点 + 引用列表 + token budget
  → Context Builder
  → Per-Agent Context Packet
```

Context Packet 只包含当前职责需要的字段：

```text
RouteNode       → query + 可用执行域
Chief Direct    → query
Chief Plan      → query + memory_context + available sources + remaining_budget
Chief Deepen    → query + plan + gaps + Evidence 摘要 + remaining_budget
DeepScout       → 当前 hypothesis + queries + 相关 Evidence 摘要 + remaining_budget
DataAnalyst     → 当前 hypothesis + sql query + datasource/dataset scope + remaining_budget
CodeWizard      → AnalysisRequest + Evidence/Dataset refs + remaining_budget
CriticMaster    → query + hypotheses + coverage + Evidence
LeadWriter      → query + hypotheses + 审核通过的 Evidence + Citation refs + write_intent
```

`remaining_budget` 是读取 `research_tasks.task_budget` 后生成的只读投影，不写入 ResearchState。

### 1.2 长内容与压缩

```text
短 ToolResult → 当前 Context + research_rounds / evidence_pool
长 Web/SQL    → Workspace + workspace_path
Document      → doc_id + chunk_id
Compute       → Workspace + execution_ref
```

Context Builder 按引用读取必要片段。压缩只处理派生摘要和历史 Observation，不覆盖：

```text
ResearchState.query
Evidence 卡片及其内容引用
原始 ToolResult
Document / Workspace 原文
```

## 2. ExecutionContext 与 claim

TaskRuntime 注入：

```text
tenant_id
user_id
task_id
thread_id
lease_token
cancellation_token
deadline
```

NodeRunner 增加 `node_run_id + node_attempt_no`。这些字段只用于执行和观测，不进入 ResearchState。

PostgreSQL 是控制面权威来源：

```text
research_tasks.status
research_tasks.lease_token
research_tasks.leased_until
research_tasks.task_budget
research_tasks.budget_accounted_at
```

首版并发约束：

```text
TENANT_MAX_RUNNING_TASKS=4
USER_MAX_RUNNING_TASKS=2
RESEARCH_WORKER_SLOTS=2
TENANT_MAX_PENDING_TASKS=100
USER_MAX_PENDING_TASKS=20
RETRIEVAL_BRANCH_CONCURRENCY=3
```

claim 在同一事务中校验 tenant/user 配额、将 `ResearchTask.status` 从 pending 改为 running、生成 `lease_token`、写入 `leased_until` 并启动运行时间结算。

## 3. TaskBudget：Runtime Guard

TaskBudget 只回答任务还能运行多久、还能搜索多少次、还能使用多少模型 Token 和金额：

```yaml
task_budget:
  budget:
    max_wall_time_ms: 1800000
    max_search_calls: 60
    max_model_tokens: 300000
    max_cost: 10.00
    currency: USD
  usage:
    wall_time_ms: 0
    search_calls: 0
    model_tokens: 0
    cost: 0
```

它是唯一权威预算记录，不复制到 ResearchState，也不另建财务账本、预算流水或 Web Tool 定价表。

### 3.1 Wall time

```text
elapsed = now() - budget_accounted_at
usage.wall_time_ms += elapsed

继续 running → budget_accounted_at=now()
离开 running → budget_accounted_at=NULL
```

`usage.wall_time_ms` 在 Task 离开 running 或续接计时段时结算。Worker 崩溃后最多结算到旧 `leased_until`。

### 3.2 reserve → execute → settle

```text
reserve(operation_key, upper_bound)
  → 锁定 ResearchTask
  → 校验本次调用不会越过上限
  → 以 operation_key 记录已预留用量

execute

settle(operation_key, actual_usage)
  → 将预留量调整为实际 usage
```

预算操作复用 NodeRun 的 operation key 幂等结果，不增加预算表。并行 fan-out 必须先 reserve；retry/replay 复用同一 key。

```text
Search Provider 真实请求 → search_calls +1
SemanticCache hit        → search_calls 不增加
Model 调用               → Provider usage 累计 model_tokens
Model cost               → model_pricing 按实际 Provider/model 换算
```

任一硬上限无法 reserve 时停止调度新调用，并记录相应 `budget_exhausted` 错误。

## 4. Agent Loop 与 ToolRuntime

```text
RouteNode / Chief / Critic / Writer
  → 一次结构化模型调用

DeepScout / DataAnalyst / CodeWizard
  → bounded ReAct
  → 最大 step、deadline、budget 和 cancel 由 Harness 注入
```

ToolRuntime 统一处理单次 Tool 调用：

```text
Policy / schema / resource scope
  → deadline / cancellation
  → Provider call
  → retry / Circuit Breaker / fallback
  → ToolResult / ToolError
```

Settings：

```text
READ_ONLY_TOOL_MAX_ATTEMPTS=2
TOOL_RETRY_BASE_DELAY_MS=250
TOOL_RETRY_MAX_DELAY_MS=2000
PROVIDER_CB_FAILURE_THRESHOLD=5
PROVIDER_CB_COOLDOWN_MS=30000
```

Circuit Breaker 按 `provider + operation` 隔离。Sandbox deadline 不得超过 Tool/Task deadline。

## 5. Pause、Resume 与 Cancel

```text
Pause
  → PG command + ResearchTask.status=pausing
  → 当前节点到达安全提交边界
  → checkpoint + ResearchTask.status=paused
  → 释放 lease

Resume
  → ResearchTask.status: paused → pending
  → research.schedule Outbox
  → Worker 以新 lease_token claim
  → 从 checkpoint 继续

Cancel
  → ResearchTask.status=cancelling
  → 停止调度新节点
  → 传播 cancellation_token
  → 终止 Sandbox
  → checkpoint + ResearchTask.status=cancelled
```

Pause、Resume 与 Cancel 只更新 ResearchTask 和 Runtime 控制记录，不写入 ResearchState。Celery Job 只携带 `task_id`，Worker 从 PostgreSQL 读取 ResearchTask、ResearchState 与 checkpoint，重建 ExecutionContext。

## 6. NodeRun 与副作用幂等

`node_run_id` 标识一次语义节点执行。retry 和 checkpoint replay 复用同一 ID；Deepen、Replan、报告 Revise 等新的语义执行使用新 ID。

```text
NodeRunner.begin
  → 在 lease fencing 下创建或读取 NodeRun
  → 固定 node_run_id、node_key、input_checkpoint_id

handler attempt
  → node_attempt_no += 1
  → 注入 Context Packet、deadline、cancellation_token

NodeRun completed
  → replay 直接读取已提交结果
```

副作用键：

```text
operation_key = sha256(tenant_id | task_id | node_run_id | effect_name)
```

同一 key 的成功结果在 retry/replay 时复用。Retrieval 分支失败写入 branch 状态并交给 Coverage；Route、Plan、Deepen、AnalysisDecision、Critic、Writer 等核心节点耗尽后，Graph 返回失败结果，由 TaskService 将 ResearchTask 标记为 failed。

## 7. 原子 Node 提交

```text
StateManager.commit_node(node_run_id, patch, expected_lease_token)
```

`patch` 只能包含当前节点拥有的 ResearchState 业务字段，不能包含 ResearchTask.status、lease、attempt 或预算字段。

同一 PostgreSQL 事务：

```text
锁定 research_tasks
  → 校验 task_id + lease_token + leased_until
  → 校验 NodeRun 尚未完成
  → 校验 patch 字段所有权
  → 将 patch 合入 research_tasks.latest_state（ResearchState）
  → 写 checkpoint
  → 写 research_event + outbox message
  → 标记 NodeRun completed
  → commit
```

Web/RAG/SQL 并行 patch 依靠字段所有权顺序合入，不依赖 ResearchState 全局版本号。

## 8. 两层恢复

### 8.1 执行层恢复

```text
PostgreSQL checkpoint
  → 恢复最后成功 Graph position

NodeRun + operation_key
  → 已完成节点和副作用直接回放

Outbox + Celery
  → 重投粗粒度 research job

lease_token
  → 隔离旧 Worker 的迟到提交
```

Worker 失联后，Recovery Scanner 在 lease 过期时把 `ResearchTask.status` 从 running 重新置为 pending 并写调度 Outbox；新 Worker 从最近 checkpoint 继续。

### 8.2 展示层恢复

```text
PostgreSQL research_events.event_id
  → 权威 replay

Redis
  → live fan-out

Frontend Last-Event-ID
  → 断线补发
  → cursor 过期时重新读取 Task Snapshot
```

执行恢复与展示恢复不共享第二套状态，也不在 checkpoint 中保存 SSE 连接信息。

## 9. 可观测

```text
research_tasks_active{status}
research_task_duration_seconds
research_task_recovery_total
node_retry_total{node}
task_budget_reserve_total{resource,result}
task_budget_used{resource}
task_budget_exhausted_total{resource}
```

核心 Span：

```text
research.run    # 一次 Worker 激活
graph.node      # 一次 handler attempt
```

同一 ResearchTask 可因 Resume 或 Worker 接管产生多个 `research.run`，通过 `task_id` 关联。

## 10. 验收标准

```text
□ Harness 只由 Context Management、Execution Runtime、State & Recovery 组成
□ 任何 Agent 都不接收完整 ResearchState
□ 原始 Query、Evidence、ToolResult 和文件内容不被上下文压缩覆盖
□ ResearchTask 是 status、claim、lease、TaskBudget、pause、resume 与 cancel 的唯一权威来源
□ ResearchState 不保存 task_status、预算、lease、attempt 或 remaining_budget
□ task_budget 只包含预算上限与累计 usage，不形成计费子系统
□ 并行调用先 reserve，retry/replay 复用 operation_key
□ 只有执行 Agent 使用有界 ReAct
□ NodeRun、State、checkpoint、Event 与 Outbox 原子提交
□ 执行恢复依靠 checkpoint/NodeRun/Outbox，展示恢复依靠 event_id/SSE replay
□ Worker 接管使用新 lease_token 隔离旧提交
```
