# DeepResearch-mutilagent：存储层（Storage）

## 0. 定位

本文只定义数据放置、权威来源、关键约束与跨存储发布流程。领域状态机、检索算法和部署参数分别由对应文档定义。

```text
PostgreSQL    → 业务事实、控制状态、元数据、Checkpoint、Event、Outbox 与 Research LTM
MinIO         → Document 原文件与 Workspace 文件字节
Elasticsearch → Document Chunk 文本和向量的可重建检索副本
Neo4j         → 当前 ResearchTask 的可重建知识图谱
Redis         → Celery broker、SSE live fan-out 与语义缓存
```

PostgreSQL 是唯一权威结构化存储。Redis 可丢失；Elasticsearch 与 Neo4j 均可从 PostgreSQL、MinIO 和 Task Evidence 重建。

部署拓扑、备份与运行参数见 [`deployment.md`](deployment.md)。

---

## 1. PostgreSQL 领域表

### 1.1 身份与对话

```text
tenants / users / tenant_user_memberships
auth_sessions                         # Refresh Session；只保存 secret hash
threads / conversation_messages       # Thread 与对话原文
```

### 1.2 Research Runtime

```text
research_tasks                        # ResearchTask Runtime Envelope + 当前 ResearchState
node_runs / node_effects               # 节点执行与副作用幂等结果
research_checkpoints                  # 不可变恢复点
research_events                       # SSE replay 的权威事件
outbox_messages                       # Job/Event 统一发布表；由 kind 区分用途
research_task_commands                # pause/resume/cancel/replan/approve/reject 请求幂等
```

`research_tasks` 是 ResearchTask 的持久化实体：普通列保存 status、lease、TaskBudget 与幂等控制，`latest_state` 保存当前 `ResearchState`。`research_checkpoints` 保存恢复历史，当前快照与恢复历史各有用途，不另建 ResearchState 表。

### 1.3 研究记忆、Document 与 Workspace

```text
research_memories                     # Research LTM + pgvector
documents                             # Document 元数据与当前发布 generation
document_ingestions                   # 每个 Document 的当前 ingestion 控制记录
document_chunks                       # Chunk identity、层级和 generation 元数据
document_upload_reservations          # Document 流式上传前的短期幂等预约
workspace_files                       # Workspace 逻辑地址与对象元数据
```

Document 是 tenant 级知识库文件，不按用户划分。原文件在 MinIO，Chunk 检索副本在 Elasticsearch。Workspace 文件的字节在 MinIO；`workspace_files` 是其元数据入口。

### 1.4 配置、数据访问与产品记录

```text
pending_writes                        # 数据库写审批、执行状态与确定性结果
model_configs / model_config_heads / model_bindings / model_config_revocations
datasources / datasource_user_grants / datasource_table_policies
shares / export_jobs
secrets                               # 仅保存加密后的 Secret
audit_logs / observability_spans
decision_traces                    # Data Flywheel 的 Tool Decision 监督记录
eval_runs / eval_case_results
model_pricing
```

PendingWrite 的执行结果保存在同一记录的终态结果中。Data Flywheel 的 Dataset Snapshot 是离线不可变产物，由 `manifest.json + train/val/test.jsonl` 表达，不预设在线 PostgreSQL 表。

---

## 2. 关键约束

```text
research_tasks
  PK(task_id)
  UNIQUE(tenant_id, user_id, create_idempotency_key)
  CHECK(status IN ('pending','running','pausing','paused','cancelling','cancelled','completed','failed'))
  INDEX(tenant_id, user_id, status)

node_runs
  PK(tenant_id, task_id, node_run_id)
  UNIQUE(task_id, node_key, input_checkpoint_id)

node_effects
  UNIQUE(tenant_id, task_id, node_run_id, effect_name)

research_checkpoints
  UNIQUE(tenant_id, task_id, checkpoint_seq)

research_events
  UNIQUE(event_id)
  INDEX(tenant_id, task_id, event_id)

outbox_messages
  UNIQUE(message_id)
  UNIQUE(tenant_id, kind, idempotency_key)

research_task_commands
  UNIQUE(tenant_id, user_id, task_id, idempotency_key)

workspace_files
  UNIQUE(tenant_id, task_id, workspace_path)
  INDEX(tenant_id, task_id, producer_tool_call_id)

pending_writes
  PK(tenant_id, write_id)
  INDEX(tenant_id, task_id, status, created_at)

documents
  PK(doc_id)
  INDEX(tenant_id, status)

document_ingestions
  PK(doc_id)
  INDEX(tenant_id, status, updated_at)
  generation + attempt_count 用于 Worker fencing

document_chunks
  PK(chunk_id)
  INDEX(tenant_id, doc_id, generation)
  chunk_id 不复用

export_jobs
  PK(export_id)
  UNIQUE(tenant_id, task_id, idempotency_key)
  INDEX(tenant_id, task_id, status)
  request_hash 用于幂等冲突检测
  attempt_count + updated_at 用于 Worker fencing 与 Recovery

shares
  PK(share_id)
  UNIQUE(public_token_hash)
  INDEX(tenant_id, task_id, expires_at)
```

`task_id`、`doc_id`、`export_id` 使用全局唯一 opaque ID，并直接作为各自聚合根主键，供只携带业务 ID 的内部消息定位记录。`tenant_id` 仍保存在业务行和子表中，用于隔离、索引与授权；对外访问不能因 ID 全局唯一而省略 tenant/user 校验。

### 2.1 ResearchTask

```text
research_tasks
  tenant_id
  user_id
  task_id
  thread_id
  status
  latest_state                   # 当前 ResearchState；不包含 task_status
  create_idempotency_key
  create_request_hash
  deleted_at?

  lease_token?
  leased_until?
  task_budget
    budget
      max_wall_time_ms
      max_search_calls
      max_model_tokens
      max_cost
      currency
    usage
      wall_time_ms
      search_calls
      model_tokens
      cost
  budget_accounted_at?
```

Task 生命周期只由 `research_tasks.status` 表达。协作暂停使用 `running → pausing → paused`，协作取消使用 `… → cancelling → cancelled`；不另存控制请求字段。`research_tasks.latest_state` 只保存 Graph 业务状态。数据库写审批由 `pending_write` 表达，降级与失败详情由 `coverage` 和 `reliability` 表达。

`lease_token` 对旧 Worker 实施 fencing，`leased_until` 为失联接管提供时间边界。`task_budget.budget` 保存任务上限，`task_budget.usage` 保存已结算的运行时间、搜索调用、模型 Token 与金额；`budget_accounted_at` 标识当前 running 计时段起点。Runtime 在一次操作内执行 `reserve → execute → settle`，并以稳定 `operation_key` 保证重放不重复结算；临时预留属于 Runtime 事务过程，不扩展为 ResearchState 字段、公开 DTO 或预算明细表。

Node 提交在事务内锁定 Task 行，并校验 `lease_token`、NodeRun 状态与 patch 字段所有权。Checkpoint 标识恢复输入，Event 的 `event_id` 负责通知去重。

NodeRun 使用稳定 `node_key` 标识 Graph 节点或分支，并以 `input_checkpoint_id` 标识本次语义输入。同一 checkpoint 可能 fan-out 到多个 `node_key`，同一 `node_key` 也可能在后续 checkpoint 再次运行，因此两者共同构成调度重放的语义唯一键。

### 2.2 Task Command 幂等

```text
research_task_commands
  tenant_id
  user_id
  task_id
  idempotency_key
  command_type                    # pause | resume | cancel | replan | approve | reject
  request_hash
  accepted_http_status
  response_json                   # 有界、脱敏的接受响应
  created_at
```

### 2.3 Document Upload 预约

```text
document_upload_reservations
  tenant_id
  idempotency_key
  doc_id
  content_hash?                   # 空值表示 reservation 仍在处理
  expires_at
  PRIMARY KEY(tenant_id, idempotency_key)
```

预约记录在流式上传完成、正式 Document 写入之前占住幂等键，因此不并入 `documents`。Document 上传操作者进入 `audit_logs`，不形成 owner scope。

### 2.4 Model Pricing

模型价格属于实际计费的 Provider Model，不属于 ModelConfig revision：

```text
model_pricing
  tenant_id
  provider
  model
  currency
  input_price_per_1m_tokens
  output_price_per_1m_tokens
  effective_at

  UNIQUE(tenant_id, provider, model, currency, effective_at)
```

P0 的 ModelRuntime 成本统一按 Token 计价；Embedding 等没有输出计费的模型使用 `output_price_per_1m_tokens=0`。PricingService 按调用时间选择 `effective_at` 不晚于该时间的最新价格。非 Token 计价能力使用 Provider 返回的实际成本，确认需要本地换算后再扩展 meter。

所有业务表都带 `tenant_id`；授权与 scope 规则见 [`auth_tenant.md`](auth_tenant.md)。

---

## 3. Redis

```text
{tenant_id}:sse:task:{task_id}     # live Stream / PubSub
{tenant_id}:ratelimit:...          # 可选短期计数
{tenant_id}:semantic-cache:...     # Tool 语义缓存；TTL 到期可直接丢失
```

SSE 重放使用 PostgreSQL `research_events`。Node 和 Tool 幂等使用 PostgreSQL `node_runs/node_effects`，Redis 不保存对应权威镜像。

语义缓存只复用相同操作约束下的 Tool 结果：

```text
SemanticCacheEntry
  tenant_id
  operation                         # web.search | document.search | sql.schema ...
  constraint_hash                   # provider、过滤条件、权限范围与 Tool 版本
  input_embedding
  payload                           # 有界、脱敏的 ToolResult
  expires_at
```

查询先按 `tenant_id + operation + constraint_hash` 过滤，再做向量近邻匹配。缓存不是 Evidence、Research LTM 或 Workspace；命中结果仍由当前调用生成新的 ToolResult/Evidence 身份，并保留原来源追溯。Redis 丢失只降低命中率，不影响研究恢复。

---

## 4. MinIO 与 Workspace

使用两个私有 Bucket：

```text
documents  → Document 原文件与上传临时对象
workspaces → Task 长结果、Compute 文件、图表、报告、Share Snapshot 与 Export
```

Workspace 对象前缀：

```text
{tenant_id}/{task_id}/
```

```text
workspace_path → Task 内逻辑地址，可进入 ResearchState、Evidence 和 API
storage_ref    → Repository 内部的 MinIO 对象引用
```

`storage_ref` 不进入 ResearchState、Evidence、Citation 或对外 API。授权下载时由服务端生成短时签名 URL 或代理返回内容。

Workspace 发布流程：

```text
写临时对象
  → 校验 hash / size / quota
  → 发布最终对象
  → 提交 workspace_files(workspace_path, storage_ref, content_hash, size, media_type, producer_tool_call_id?)
```

对象存储与 PostgreSQL 之间不存在跨系统原子事务。发布失败由临时对象清理和 orphan reconciliation 收敛。

报告、导出和分享仍使用 Workspace：

```text
reports/versions/{report_version}/report.json
reports/versions/{report_version}/report.md
reports/exports/{export_id}.docx
reports/exports/{export_id}.pdf
reports/shares/{share_id}/snapshot.json
```

Compute 使用固定 bundle：

```text
compute/{tool_call_id}/code.py                  # 仅 sandbox_execute
compute/{tool_call_id}/manifest.json
compute/{tool_call_id}/outputs/**
```

`workspace_files.producer_tool_call_id` 仅对 Compute bundle 文件设置，用于从任一输出定位 lineage manifest；输入证据、代码 hash、运行环境与输出 hash 保存在 manifest，不新增 Compute 运行表。

`export_jobs` 保存导出控制状态、固定报告版本、幂等字段、attempt 和输出 `workspace_path`；`shares` 保存固定 final report version、Token hash、有效期与冻结快照 `workspace_path`。文件 hash、大小、媒体类型和物理引用统一由 `workspace_files` 管理。

Workspace 详细合同见 [`research_workspace.md`](../02_capabilities/research_workspace.md)，分享与导出见 [`share_export.md`](../06_product/share_export.md)。

---

## 5. Document 检索副本

```text
PostgreSQL documents/document_chunks → Document、Chunk identity 与发布 generation
MinIO documents Bucket              → 原始文件
Elasticsearch document_chunks       → Chunk 文本、dense_vector 与检索过滤字段
```

Elasticsearch 中的文本和向量是可重建副本。检索只读取 PostgreSQL `published_generation` 对应的 Chunk；新的 generation 完成解析、Embedding 和索引发布后再切换可见 generation。

BM25、kNN、RRF、Rerank 与 Parent Expansion 由 [`rag.md`](../02_capabilities/rag.md) 定义；Storage 只定义其数据位置和发布边界。

---

## 6. Research LTM 与 Task Knowledge Graph

Research LTM 的权威记录和向量都位于 PostgreSQL `research_memories + pgvector`，scope 为 `tenant_id + user_id`。记忆写入、冲突、召回和遗忘规则见 [`memory.md`](../02_capabilities/memory.md)。

Neo4j 保存当前 ResearchTask 的派生知识图谱：

```text
namespace = tenant_id + task_id
Entity / Claim / Relation → evidence_id → ResearchState.evidence_pool
```

知识图谱可由当前 Query、Plan 和 Task Evidence 重建，不作为 Evidence 或 Citation 的第二事实源。

---

## 7. Outbox 与跨存储发布

```text
outbox_messages
  tenant_id
  message_id
  kind
  aggregate_id                  # task_id | doc_id | export_id | event_id
  payload_json
  idempotency_key
  available_at
  dispatched_at?
  created_at
```

`outbox_messages.kind` 区分以下消息：

```text
research.schedule
research.event.publish
document.ingest
report.export
knowledge_graph.build
```

业务记录与对应 Outbox Message 在同一 PostgreSQL 事务提交；Dispatcher 以 at-least-once 方式发布，消费者通过领域 claim、稳定业务 ID 和幂等键处理重复消息。

MergeEvidence 提交新增 Evidence 时，在同一事务写入 `knowledge_graph.build` Outbox Message。Dispatcher 将其投递到现有 `research` 队列，由 KG Builder 幂等更新当前 Task 的 Neo4j namespace。该消息是 Task KG 非阻塞构建的 durable trigger，不新增 KG 队列、KG 状态表或 ResearchState 字段。

### 7.1 Document

```text
创建 document_upload_reservations
  → 流式写 MinIO 临时对象并计算 content_hash
  → 发布原始对象
  → PG 事务 finalize Document + document_ingestions + document.ingest Outbox
  → Worker 处理当前 generation
  → 写 Elasticsearch 候选 Chunk
  → 发布完成后更新 documents.published_generation
```

对象写入成功而 PG finalize 失败时执行 best-effort 删除；未删除对象由 orphan reconciliation 清理。

### 7.2 Workspace

Workspace 生产者统一使用 §4 的发布入口。短 Web/SQL 结果直接进入 Evidence；长 Web/SQL 结果及 Compute 文件写入 Workspace，Evidence 保存 `workspace_path + content_hash`。

### 7.3 派生存储恢复

```text
Elasticsearch → 从 Document 原文件、Chunk 元数据和当前 generation 重建
Neo4j         → 从当前 Task 的 Query、Plan 与 Evidence 重建
Redis         → 从 PostgreSQL Event/控制记录恢复 live 服务
```

---

## 8. 验收标准

```text
□ ResearchState 当前值只存于 research_tasks.latest_state，恢复历史存于 research_checkpoints
□ Task 生命周期由 research_tasks.status 表达，latest_state 不保存 task_status
□ pause 使用 running → pausing → paused，cancel 使用 cancelling；无第二控制字段
□ Node 提交以 lease_token、NodeRun、字段所有权和事务行锁保证一致性
□ SSE 使用 event_id 去重和重放，Runtime 使用 Checkpoint 恢复
□ PendingWrite 的确定性执行结果保存在 pending_writes
□ Workspace 元数据统一使用 workspace_path，物理 storage_ref 不进入外部合同
□ Share 保存 final report Snapshot 控制字段；ExportJob 以 request_hash 和 attempt_count 保证幂等与 fencing
□ Document 以 tenant_id 隔离，不按上传用户划分；上传预约不承担 Document 授权
□ model_pricing 按 provider + model + 生效时间定价
□ task_budget 只保存 budget 与已结算 usage，是 TaskBudget 的唯一权威记录；remaining_budget 仅为运行时只读投影
□ SemanticCache tenant-scoped、带 TTL，且不充当 Evidence 或幂等存储
□ MergeEvidence 与 knowledge_graph.build Outbox 同事务提交，Worker 崩溃不会丢失 KG 构建触发
□ Redis、Elasticsearch 与 Neo4j 均可从权威存储恢复
□ MinIO/PG 发布失败可由临时对象清理和 orphan reconciliation 收敛
```
