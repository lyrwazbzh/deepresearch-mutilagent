# Database Write：WritePlan → HITL → WriteExecutor

## 0. 定位

业务数据库写入使用结构化计划、持久化审批和确定性事务执行。端到端流程以 §1 为准。

DataAnalyst 决定需要修改的业务对象与值；PrepareWrite 生成可审批记录；WriteExecutor 根据已批准计划构造参数化 SQL 并执行事务。

DataSource、Grant、Policy 与 Credential 合同见 [`datasource.md`](datasource.md)，ResearchState 字段见 [`state.md`](../01_architecture/state.md)，只读数据库 Tool 见 [`execute_readonly_sql.md`](tools/data/execute_readonly_sql.md)。

---

## 1. 端到端流程

```text
用户提出数据库写入请求
  → Plan.write_intent
  → Retrieval / MergeEvidence / AnalysisDecision / Compute / Coverage
  → Critic pass + score >= 8
  → DatabaseWriteGate
       ├─ write_intent 不存在 → LeadWriter
       └─ write_intent 存在
            → PrepareWrite
            → 同一事务创建 PendingWrite、暂停 ResearchTask、提交 checkpoint 与 approval outbox
            → 用户 Approve 或 Reject

Reject
  → PendingWrite.status = rejected
  → decision + Audit
  → 清理 ResearchState.pending_write
  → ResearchTask.status: paused → pending
  → checkpoint + research.schedule Outbox

Approve
  → PendingWrite.status = approved
  → decision + Audit
  → WriteExecutor
  → CAS approved → executing
  → ExecutionAuthorization.resolve
  → 外部业务库事务
  → PendingWrite.status = executed | failed | stale
  → WriteResult + Audit
  → 清理 ResearchState.pending_write
  → ResearchTask.status: paused → pending
  → checkpoint + research.schedule Outbox
```

`write_intent` 只记录用户的写入意图。PrepareWrite 只能在研究与可选 Compute 完成、Coverage 结构完整且 Critic 以 `score >= 8` 通过后，由 DatabaseWriteGate 进入。

---

## 2. WritePlan

WritePlan 是待执行变更的结构化内容：

```text
WritePlan {
  plan_id: string
  datasource_id: string
  operation: insert | update | delete
  table: string

  values?: object
  filters?: FilterPredicate[]
  reason: string
  preconditions?: object
}
```

```text
FilterPredicate {
  column: string
  op: "=" | "!=" | ">" | ">=" | "<" | "<=" | "IN"
  value: scalar | scalar[]
}
```

字段规则：

```text
INSERT
  values 必填

UPDATE
  values 必填
  filters 必填

DELETE
  filters 必填
```

`preconditions` 由 PrepareWrite 根据数据源支持情况写入，用于携带可选的旧值或 version 条件。

示例：

```json
{
  "plan_id": "wp_123",
  "datasource_id": "erp_db",
  "operation": "update",
  "table": "purchase_orders",
  "values": { "status": "cancelled" },
  "filters": [
    { "column": "order_id", "op": "=", "value": "PO123" }
  ],
  "reason": "用户要求取消采购订单 PO123",
  "preconditions": { "version": 3 }
}
```

WriteExecutor 使用结构化字段生成 SQL；表名和列名来自已验证的 Policy，值和过滤值使用 parameter binding。

---

## 3. PrepareWrite

PrepareWrite 是数据库写路径的审批准备阶段，依次完成计划规范化、策略校验、预览生成和 PendingWrite 持久化。

### 3.1 WritePolicyValidator

WritePolicyValidator 根据当前 DataSource Policy 校验：

```text
operation
table
writable_columns
filter requirements
allow_insert / allow_update / allow_delete
```

校验成功后记录 `policy_revision`。`max_write_rows` 由该 Policy 管理，并在事务执行时作为实际影响行数上限。

### 3.2 Preflight 与 WritePreview

Preflight 根据规范化 WritePlan 构建可直接审批的摘要：

```text
WritePreview {
  datasource
  operation
  table
  filters?
  values?
  reason
  current_version?
  expected_value?
}
```

```text
WritePlan
  → 格式化 datasource / operation / table / filters / values / reason
  → 数据源配置了 version 或并发字段时读取对应当前值
  → 将 version / expected value 写入 preconditions
  → WritePreview
```

示例：

```text
数据源：ERP
操作：UPDATE
表：purchase_orders
条件：order_id = PO123
变更：status = cancelled
当前版本：3
原因：用户要求取消采购订单 PO123
```

PrepareWrite 对规范化后的 Plan、`preconditions` 和 `policy_revision` 计算 `plan_hash`，审批绑定该哈希。

### 3.3 创建 PendingWrite

PrepareWrite 在一个状态提交事务中完成：

```text
INSERT PendingWrite(status=pending)
+ ResearchState.pending_write = write_id
+ ResearchTask.status = paused
+ checkpoint
+ research_event(write.approval.requested)
+ outbox_messages(kind=research.event.publish)
```

---

## 4. PendingWrite

PendingWrite 保存一次待审批数据库写入的领域状态：

```text
PendingWrite {
  write_id: string
  task_id: string
  tenant_id: string
  user_id: string

  plan: WritePlan
  plan_hash: string
  preview: WritePreview
  policy_revision: string

  status: pending | approved | executing | rejected | executed | failed | expired | stale
  created_at
  expires_at?

  decision?: {
    action: approved | rejected
    actor_id: string
    at: datetime
    reason?: string
  }

  operation_key: string
  result?: WriteResult
}
```

```text
operation_key = sha256(tenant_id|write_id|database_write:v1)
```

PendingWrite 的 `status` 使用 CAS 推进。Worker、lease、attempt 与 fencing 信息由 Runtime NodeRun 管理。

---

## 5. HITL 与 Approval API

审批绑定 `write_id + plan_hash`，确保用户确认的 Preview 与最终执行计划一致。

```text
GET  /api/v1/tasks/{task_id}/pending-writes/{write_id}

POST /api/v1/tasks/{task_id}/pending-writes/{write_id}/approve
  body: { plan_hash, comment? }

POST /api/v1/tasks/{task_id}/pending-writes/{write_id}/reject
  body: { reason? }
```

服务端先校验父级 Task Ownership，再校验 PendingWrite 属于路径中的 `task_id`，并将批准或拒绝写入统一 `decision`。

审批成功后发布 `task_updated`（`changed` 包含 `pending_write` 与 ResearchTask `status`），驱动前端刷新审批卡和 Task 投影。

---

## 6. WriteExecutor

### 6.1 领取与当前授权

```text
execute(write_id, plan_hash)
  → 校验 PendingWrite.status = approved
  → 校验提交的 plan_hash
  → CAS approved → executing
  → ExecutionAuthorization.resolve(PendingWrite)
```

ExecutionAuthorization 解析当前执行条件：

```text
datasource 当前可用
user write grant 当前有效
PendingWrite.policy_revision 仍为当前 Policy revision
CredentialResolver 解析当前 write credential
```

Policy revision 变化时，PendingWrite 进入 `stale`，并重新经过 PrepareWrite 与 HITL。Plan 中的 operation、table 和 columns 已由对应 `policy_revision` 校验，Executor 使用该批准版本的编译结果。

### 6.2 事务执行

```text
ExecutionAuthorization
  → 构造参数化 SQL
  → BEGIN
  → 使用 operation_key claim 外部幂等流水
  → 将 preconditions 合入 WHERE
  → 执行 INSERT | UPDATE | DELETE
  → 读取 affected_rows
  → 对照 Policy.max_write_rows
  → 写入外部幂等结果
  → COMMIT 或 ROLLBACK
  → WriteResult
```

`affected_rows = 0` 且存在 precondition 时，结果为 `stale_write_plan`。实际影响行数超过 `Policy.max_write_rows` 时事务回滚。

### 6.3 外部幂等流水

目标业务库使用与 DML 相同的事务保存幂等结果：

```text
agent_write_idempotency
  operation_key      PRIMARY KEY
  plan_hash
  affected_rows
  result_json
  committed_at
```

```text
BEGIN
  → INSERT operation_key + plan_hash
  → 已存在时 SELECT ... FOR UPDATE
  → 首次 claim 时执行参数化 DML
  → 写 affected_rows 与 result_json
COMMIT
```

显式配置稳定业务唯一键的 INSERT 可以使用目标表 UNIQUE 约束作为幂等依据；UPDATE 和 DELETE 使用外部幂等流水。

### 6.4 Worker 恢复

```text
Runtime NodeRun lease 过期
  → 新 Executor 接管执行
  → 使用相同 write_id / operation_key 查询外部幂等结果
  → 已提交：复用 WriteResult
  → 待执行：重新进入事务
```

外部 `operation_key` 唯一约束负责串行化重叠执行。

---

## 7. WriteResult 与 ResearchState

```text
WriteResult {
  write_id
  plan_id
  operation
  datasource_id
  table
  affected_rows
  status: success | failed | stale | rejected
  executed_at?
}
```

等待审批时，`ResearchState.pending_write` 关联当前 `write_id`，`ResearchTask.status=paused`。ResearchState 不保存 `task_status`。

审批拒绝或执行进入终态后，在状态提交事务中保存 PendingWrite 的 `decision/result`、清理 `ResearchState.pending_write`、将 `ResearchTask.status` 从 `paused` 更新为 `pending`，并提交 checkpoint 与 `research.schedule` Outbox；后续 Worker 从 checkpoint 继续。

---

## 8. Audit 与可观测

```text
agent:data_analyst
  → action=pending_write.propose, result=succeeded

user:{id}
  → action=pending_write.approve | pending_write.reject
  → result=succeeded | denied | failed

system:write_executor
  → action=pending_write.execute
  → result=succeeded | failed

policy:write_policy
  → action=pending_write.execute, result=denied
```

Audit 使用 [`observability.md`](../05_quality/observability.md) 的统一合同：

```text
audit_id
tenant_id
actor_ref
action
resource_ref = pending_write:{write_id}
result
reason_code?
trace_id?
created_at
```

WriteExecutor 使用 `graph.node` 表达 handler attempt，实际业务库事务使用标准 Database CLIENT Span。WritePlan、Policy revision、审批决定和 `affected_rows` 保存在 PendingWrite；Trace 使用原生 status / `error.type` 表达执行结果，不复制领域记录。

---

## 9. 结果分类

| 情况 | 结果 |
|---|---|
| plan_hash 与审批不一致 | `plan_hash_mismatch` |
| DataSource、Grant 或 Policy revision 已变化 | `stale_write_plan` |
| precondition 未命中 | `stale_write_plan` |
| affected_rows 超过 Policy.max_write_rows | `write_limit_exceeded` + rollback |
| 外部 operation_key 对应不同 plan_hash | `idempotency_conflict` + rollback |
| 数据库执行异常 | `database_write_failed` + rollback |

---

## 10. 测试

```text
□ INSERT / UPDATE / DELETE 生成正确参数化 SQL
□ PrepareWrite 仅能由 Critic pass + score>=8 后的 DatabaseWriteGate 进入
□ PrepareWrite 按 Policy 校验 operation、table、columns 与 filters
□ Preview 展示 datasource、operation、table、filters、values 与 reason
□ configured version / expected value 写入 preconditions
□ PendingWrite、ResearchState.pending_write、ResearchTask.status=paused、checkpoint 与 outbox 同事务提交
□ Approve 绑定 write_id + plan_hash；Reject 绑定 write_id
□ ExecutionAuthorization 使用当前 datasource、grant、policy_revision 与 credential
□ Policy revision 变化后 PendingWrite 转为 stale
□ precondition 未命中时 rollback
□ affected_rows 超过 Policy.max_write_rows 时 rollback
□ 并发 Executor 通过 CAS 与 operation_key 只提交一次 DML
□ Worker 接管后复用外部幂等结果
□ 执行结果写入 PendingWrite、WriteResult 与 Audit
□ 拒绝或执行终态时清理 pending_write，并以 ResearchTask.status=pending + checkpoint/outbox 恢复调度
```

---

## 11. 验收标准

```text
□ 文档只有一条 WritePlan → PrepareWrite → PendingWrite → HITL → WriteExecutor 主流程
□ PrepareWrite 只能在 Critic 通过后的 DatabaseWriteGate 中进入
□ PrepareWrite 统一完成策略校验、Preview、plan_hash 与 PendingWrite 创建
□ WritePlan 保存变更内容，Policy.max_write_rows 保存影响行数上限
□ Preflight 生成简化 Preview，并按数据源配置读取可选并发条件
□ PendingWrite 使用 decision 表达批准或拒绝，Runtime NodeRun 管理执行租约
□ Approval API 使用 task_id/write_id 资源路径并提交 plan_hash
□ ExecutionAuthorization 一次解析当前 datasource、grant、policy revision 与 credential
□ WriteExecutor 使用参数化 SQL、precondition、Policy.max_write_rows 与外部 operation_key
□ ResearchState.pending_write 连接暂停任务与 PendingWrite
□ Task 生命周期只由 ResearchTask.status 表达，ResearchState 不保存 task_status
□ WriteResult、Audit、checkpoint 与恢复流程闭环
```
