# DeepResearch-mutilagent：Authorization & Multi-tenancy

## 0. 文档职责

本文只回答：在执行某个动作前，当前用户允许读取或操作哪些候选资源。

- Research LTM 的召回、排序与注入写在 [`memory.md`](../02_capabilities/memory.md)。
- Document RAG 的 BM25、kNN、RRF、Rerank 与 provenance 写在 [`rag.md`](../02_capabilities/rag.md)。
- JWT、凭据和网络安全写在 [`auth_security.md`](auth_security.md)。

```text
AuthContext = tenant_id + user_id + roles[]
```

具体资源标识由路由、队列消息或服务端记录提供，不是 JWT 字段。

---

## 1. 授权根与继承关系

P0 使用少量授权根，子资源继承父级授权结果：

```text
Thread
  └─ Message

ResearchTask
  ├─ ResearchState / Checkpoint / Event
  ├─ Evidence / WorkspaceFile / Task KG
  └─ PendingWrite / ExportJob

Document
  ├─ Document Chunk / Elasticsearch index document
  └─ original object

Research LTM
  └─ memory rows

DataSource
  └─ Grant / Policy / connection

ModelConfig
  └─ revision / secret

Share
  └─ frozen snapshot
```

“知识库”是 Document 集合的产品名称，不是独立授权资源。Task 和 Thread 在 P0 为 owner-only；同租户协作能力等真实需求出现后再增加显式共享模型。

---

## 2. 授权规则

### 2.1 Task、Thread 与子资源

```text
ResearchTask / ResearchState / Checkpoint / Event
  → tenant_id + user_id + task_id

Thread / Message
  → tenant_id + user_id + thread_id

Workspace File / Evidence / Task KG / PendingWrite / ExportJob
  → 先按 task_id 加载 ResearchTask
  → 继承该 Task 的 tenant_id + user_id 授权
```

Workspace 路径只在 Task 已授权后规范化和解析；路径本身不是授权主体。

### 2.2 Document

Document 是 tenant 级知识库资源，以 `tenant_id + doc_id` 定位：

```text
tenant_id = AuthContext.tenant_id
当前 user 是该 tenant 的 active member
写入、删除等变更操作再校验对应 role
```

Document Chunk、Elasticsearch 索引记录与原始对象继承 Document 的 tenant 边界。上传者只进入 Audit，不形成 owner scope。显式 `doc_ids`、`tags` 等检索条件只能缩小候选范围。

### 2.3 Research LTM

```text
tenant_id = AuthContext.tenant_id
user_id   = AuthContext.user_id
status    = active
```

这是 MemoryService 可以参与召回的候选边界；Topic、相关度、冲突、过期和 Token Budget 由 [`memory.md`](../02_capabilities/memory.md) 决定。

### 2.4 DataSource 与 ModelConfig

```text
DataSource
  → tenant_id + datasource_id
  → admin 管理连接与 Policy
  → 用户通过 datasource_user_grants 获得 read | write

ModelConfig
  → tenant_id + model_config_id
  → admin 管理配置与凭据
  → Task 仅使用已启用且对当前租户可用的配置
```

Task 创建时可以校验选择的 DataSource；SQL 实际执行时重新校验 `enabled + Grant + Policy`，之后才由 Adapter 按需解析凭据。

### 2.5 Share

私有 Share 管理接口继承 owner Task 的授权。公开 Token 的读取范围固定为冻结 Snapshot 及其 manifest Assets；Task、Document 和 Workspace 继续使用各自私有授权。

---

## 3. 各存储后端的执行方式

### 3.1 PostgreSQL

Repository 在授权根查询中同时使用租户与 owner/grant 条件。子资源先解析父级，再执行业务读写。

```sql
SELECT * FROM research_tasks
WHERE tenant_id = :tenant_id
  AND user_id = :user_id
  AND task_id = :task_id;
```

### 3.2 Elasticsearch Document Chunk

BM25 与 kNN 在各自 Top-K 召回前应用相同的 Document 访问过滤：

```text
tenant_id = T
```

显式 `doc_ids`、`tags` 与上述候选集合取交集。授权层到此为止；RRF、Rerank、Parent Expansion 与 provenance 由 [`rag.md`](../02_capabilities/rag.md) 定义。

### 3.3 MinIO 与 Workspace

```text
Document object
  → 先授权 Document
  → 再解析 documents/{tenant_id}/{doc_id}/... 对象键

Workspace object
  → 先授权 ResearchTask
  → 再规范化 workspace_path
  → 解析私有 workspaces Bucket 的 {tenant_id}/{task_id}/... 对象键
```

签名 URL 只在上述授权完成后生成，并使用短 TTL。Workspace 是 Task 工作记忆抽象，底层由 MinIO 实现，不形成另一套权限系统。

### 3.4 Neo4j Task KG

Task KG 是当前研究问题的派生索引。查询先授权父级 ResearchTask，再限定 `tenant_id + task_id` namespace。`user_id` 属于父 Task 的 owner 信息，不是图身份的一部分。

### 3.5 Redis

Redis key/stream 使用 tenant/task 前缀避免碰撞并便于运维隔离。业务授权始终在访问持久化资源前完成，Redis 前缀本身不构成 Authorization。

---

## 4. Task ID 与 ExecutionContext

`task_id` 的来源随入口确定：

```text
POST /tasks
  → TaskService 生成 task_id
  → 在创建事务中写入 Message、Task、初始 State、checkpoint 与 outbox

HTTP Task 路由
  → 从 path 取得 task_id
  → 按 tenant_id + user_id + task_id 加载 Task

Research Worker
  → 队列消息只提供 task_id
  → Worker 重载 Task，恢复 tenant_id / user_id / thread_id
  → 成功 claim 后加入 lease_token / cancellation_token / deadline

Task 子资源
  → 从子资源记录解析 task_id
  → 回到父 Task 授权
```

不同入口构造与职责匹配的上下文：

```text
HTTP request       → AuthContext(tenant_id, user_id, roles[])
Task runtime       → tenant_id, user_id, thread_id, task_id, lease_token, cancellation_token, deadline
Document ingestion → tenant_id, doc_id
Export             → tenant_id, user_id, task_id, export_id
```

上下文只携带已经从权威记录解析出的标识；它不是新的公共 DTO，也不缓存长期授权结论。

---

## 5. 审计与验收

授权失败审计记录 action、授权根类型、tenant_id、user_id、结果和稳定错误码；具体资源标识按审计策略记录或哈希化。日志与 Trace 不记录 Token、Secret 或签名 URL。

```text
□ Task/Thread owner-only 规则覆盖根资源和全部子资源
□ Document tenant 规则同时约束 PG、BM25 与 kNN 候选
□ 跨租户 Document 检索集成测试返回零命中
□ MinIO 对象键只由已授权的 Document 或 Task 解析
□ Neo4j 查询先授权 Task，再限定 tenant_id + task_id
□ DataSource 在每次 SQL 执行前校验 enabled、Grant 与 Policy
□ task_id 始终来自创建、路由、队列或父资源记录，不来自 JWT
□ Research LTM 与 Document 授权只定义候选，召回/排序留在各自能力文档
```

## 6. 实现顺序

1. Task、Thread、Document、DataSource、ModelConfig、Share 授权根 Repository。
2. 子资源父级解析器与统一 404 行为。
3. Elasticsearch BM25/kNN 一致 pre-filter 和跨租户集成测试。
4. MinIO object resolver、Workspace path guard 与短时签名 URL。
5. Neo4j Task namespace guard 与跨 Task 隔离测试。
