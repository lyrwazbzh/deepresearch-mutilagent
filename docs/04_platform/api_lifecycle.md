# DeepResearch-mutilagent：API / 任务生命周期

## 0. 合同位置

```text
UI（[`frontend.md`](../06_product/frontend.md)）
  ↕  HTTP / SSE（本文）
Service / API
  ↕  启停、投影、ownership
ResearchTask
  ↕  status / 调度 / pause / resume / cancel
LangGraph Runtime（graph.md）
  ↕  patch / checkpoint
ResearchState（[`state.md`](../01_architecture/state.md)）
```

```text
Checkpoint     → GET task 读取已提交 State；
                 resume 从 checkpoint 的 LangGraph graph position 恢复
可观测 / SSE   → Event 先落 outbox，再推流；带稳定 event_id
Auth           → 默认 JWT + OwnershipGuard；Auth 与公开 Share 例外见 §2.5
```

**原则：**

```text
研究任务 UI 面向 Task / Thread / Event / Document / Evidence / Workspace File / DataSource 等产品资源。

产品界面的“知识库”直接使用 Document API。
```

---

## 1. 资源模型

```text
Thread（会话）
  tenant_id + user_id + thread_id
  → 可挂 0..N 个 ResearchTask

ResearchTask（研究任务）
  tenant_id + user_id + thread_id + task_id
  → status 是任务生命周期的唯一事实源
  → 一份 ResearchState + checkpoints + events
  → 一次逻辑 Research Graph 执行实例，可 pause/resume

ResearchState（Graph 业务状态）
  → 保存 query / route / plan / evidence / coverage / critic / report
  → 不保存 task_status

Message（聊天消息）
  属 Thread；user 立即落库，assistant 可见回复再落库（memory.md）

WorkspaceFile
  属 ResearchTask；保存当前 Task 的工作记忆与生成文件
  大对象字节在 MinIO；API 只暴露受控 path/file ref

Event
  已持久化的任务执行投影；SSE 只推已提交 Event（含 event_id）
```

主流程：

```text
一次研究请求
  → 确保 Thread 存在
  → POST /tasks（thread_id + query）
       Service：原样 persist user Message(query)
              + 由「本次 query + 同 Thread 近期对话」补全 ResearchState.query
              + create ResearchTask
  → GET /tasks/{id}/events 建立 SSE（Event 驱动 UI）
  → GET /tasks/{id} 用于初始加载 / 刷新恢复
  → RouteNode 选择 direct / focused / deep
       direct/focused → assistant Message
       deep           → 按 `report.final.workspace_path` 读取 Workspace 报告
```

普通聊天、单 Agent 工具任务与深度研究使用同一个 `POST /tasks` 入口；RouteNode 决定执行路径，避免客户端预判任务复杂度。

---

## 2. HTTP 路由（首期）


### 2.1 Research Task

| 方法 | 路径 | 作用 |
|------|------|------|
| `POST` | `/tasks` | 落库 user Message + 创建 Task；调度 Graph |
| `GET` | `/tasks/{task_id}` | 任务投影 DTO（§3.1） |
| `GET` | `/tasks` | 列表（thread / task_status 过滤） |
| `POST` | `/tasks/{task_id}/cancel` | 协作取消 |
| `POST` | `/tasks/{task_id}/pause` | 协作暂停（请求受理 ≠ 立刻 paused） |
| `POST` | `/tasks/{task_id}/resume` | 无待审批写入时从 checkpoint pending 续跑 |
| `POST` | `/tasks/{task_id}/replan` | 运行中改目标（§6） |
| `POST` | `/tasks/{task_id}/data-files` | 上传当前 Task 的 XLSX/CSV/Parquet，保存为 SQL/Data 输入（§2.1.7） |
| `DELETE` | `/tasks/{task_id}` | 软删除终态 Task；立即隐藏并异步清理 Task-scoped 数据 |
| `GET` | `/tasks/{task_id}/evidence` | 当前 Task Evidence 分页列表（§2.1.4） |
| `GET` | `/tasks/{task_id}/evidence/{evidence_id}` | 单项 Evidence 卡片（§2.1.4） |
| `POST` | `/tasks/{task_id}/knowledge-graph/query` | 有界查询当前 Task KG（§2.1.5） |

`POST /tasks` body：

```text
Idempotency-Key: <client-generated-key>     # 必填

{
  "query": "...",              # 本次用户原文；作为该 Thread 的 user Message 原样持久化
  "thread_id": "...",          # 可选；缺省新建 thread
  "options": { "max_depth": 3, "max_revision_rounds": 2 }
}
```

```text
max_depth             默认 3；硬上限 5（传入 >5 → 钳为 5；<0 → 422 invalid_argument）
max_revision_rounds   默认 2；硬上限 5（传入 >5 → 钳为 5；<0 → 422 invalid_argument）
钳制发生在校验 CreateTask DTO 时，写入 State.execution 的是钳制后的值
request_hash 对钳制后的完整 DTO 计算（与 §2.1.1 一致）
```

合同：

```text
POST /tasks
  → 校验并钳制 CreateTask DTO，对规范化、钳制后的完整 DTO 计算 request_hash
  → 以本次用户消息为截止点，按固定消息数与 token budget 读取同 Thread 的持久化 user/assistant 消息
  → 本次用户输入 + 同 Thread 近期对话 → 补全为完整查询
  → 同一 PostgreSQL 事务：
       ensure/create Thread
       persist user Message(request.query)
       generate task_id
       create ResearchTask(status=pending)
       write ResearchState(query=完整查询) into latest_state
       create checkpoint₀
       write research.schedule Outbox Message
  → 201 Created + { task_id, thread_id, message_id, task_status, ... }
```

#### 2.1.1 Task Creation Idempotency Contract


```text
create_idempotency_key
create_request_hash
UNIQUE(tenant_id, user_id, create_idempotency_key)
```

```text
新 key
  → 原子创建 Message / Task / State / checkpoint₀ / research.schedule Outbox Message
  → 201 Created

相同 key + 相同 request_hash
  → 返回原 task_id/thread_id 的当前投影
  → 200 OK

相同 key + 不同 request_hash
  → 409 idempotency_conflict

相同 key 指向 soft-deleted Task
  → 410 idempotency_target_deleted
```


#### 2.1.2 Task 删除

```text
DELETE /tasks/{task_id}
  terminal(cancelled|completed|failed)
    → 同一事务写 research_tasks.deleted_at=now()
       + 撤销该 Task 的 active Share
       + pending/running ExportJob → failed(error_code=task_deleted)
    → Task 与公开 Share 立即从读取入口隐藏
    → Workspace/checkpoint/event 进入异步清理；知识库 Document 不随 Task 删除
    → 202 Accepted

  pending|running|pausing|paused|cancelling
    → 409 invalid_state；调用方先 cancel 并等待 cancelled
```


#### 2.1.3 GET 投影（UI 合同）

```text
task_id, thread_id
task_status
route                    # { mode: direct|focused|deep, target_agent? }
pending_write?           # 业务写 HITL：PendingWrite.write_id；仅等待写审批时出现
phase                    # routing|planning|retrieving|analyzing|reviewing|writing|completed|…
report.draft?             # { version, workspace_path, content_hash }
report.final?             # { version, workspace_path, content_hash }
updated_at
```

TaskProjection 是 ResearchTask 与 ResearchState 的组合投影：

```text
task_id / thread_id / task_status ← ResearchTask
phase                            ← Graph / checkpoint / Runtime 投影
route / pending_write / report   ← ResearchState
updated_at                       ← 当前组合投影的更新时间
```

对外字段继续使用 `task_status`，其唯一来源是 `ResearchTask.status`。审批、降级与失败详情分别读取 ResearchState 的 `pending_write`、`coverage` 和 `reliability`。

#### 2.1.4 Evidence（UI 合同）

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/tasks/{task_id}/evidence?source_type=&limit=&cursor=` | Evidence 卡片分页 |
| `GET` | `/tasks/{task_id}/evidence/{evidence_id}` | 读取一项 Evidence |

Evidence 响应保持 [`state.md`](../01_architecture/state.md) 的字段组合：短结果返回完整 `data`；Document、Workspace 与 Web 来源返回相应稳定引用。接口不把 Workspace 长文件或 Document Chunk 复制进响应；客户端根据引用调用对应内容端点。

#### 2.1.5 Knowledge Graph 查询

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/tasks/{task_id}/knowledge-graph/query` | 有界执行 neighbors / path / search，返回 Entity、Claim、Relation 与 `evidence_id` provenance |

请求与响应复用 [`query_knowledge_graph.md`](../02_capabilities/tools/knowledge_graph/query_knowledge_graph.md) 的业务字段。路径中的 `task_id` 是唯一公共图标识；Task KG 是按当前研究问题构建的 Evidence 派生索引。API 返回 `evidence_id`，来源正文仍由 Evidence、Document 或 Workspace 解析。

#### 2.1.6 Workspace 浏览与文件读取

前端「搜索结果 / 可视化」Tab 通过下列 Workspace API 读取工作记忆与生成文件：

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/tasks/{task_id}/workspace` | 按 `prefix` 或 `glob` 列出当前 Task 的 Workspace 文件 |
| `GET` | `/tasks/{task_id}/workspace/{workspace_path:path}` | 读取文本或二进制文件；支持 HTTP Range 与 `?download=true` |

服务端执行 Task Ownership、path guard 与文件元数据校验后代理响应，并依据 `media_type` 设置 Content-Type。文本范围读取使用标准 Range；下载时设置 Content-Disposition。

```text
浏览例：
  GET .../workspace?prefix=files/
  GET .../workspace/files/web-page.md
  GET .../workspace/visualizations/chart.png?download=true
```

非法 path、`..`、绝对路径、跨 task → 统一 `404 not_found`。

#### 2.1.7 Task Data File 上传

```text
POST /tasks/{task_id}/data-files
Content-Type: multipart/form-data
Idempotency-Key: <client-generated-key>

file = 单个 XLSX | CSV | Parquet 文件
```

处理流程：

```text
JWT + Task Ownership
  → 在 PostgreSQL 创建 workspace_files(status=pending) 上传 reservation
       持久化规范化的上传幂等 scope/key（scope 由 tenant_id + user_id + task_id + endpoint 组成）
  → 流式写入 workspaces Bucket 临时对象并计算 content_hash
  → 校验文件类型、原始文件大小与 Task/Tenant quota
  → WorkspaceService.finalize_file
       path = files/data/{server_generated_id}/{sanitized_name}
       CAS workspace_files.status: pending → ready
  → 201 Created
       { workspace_path, content_hash, media_type, size }
```

应用层对原始文件执行精确的 `256 MiB` 上限。该接口只创建 Task-scoped Workspace File，不创建 Document 或新的顶层 Dataset 资源。SQL/Data 分支使用 `workspace_path` 读取文件：CSV/XLSX 可按需规范化为 Parquet，XLSX 每个 Sheet 作为一个逻辑表，DuckDB 在查询时建立临时只读 View；这些均为 SQL/Data 内部实现。

`Idempotency-Key` 的逻辑范围为 `tenant_id + user_id + task_id + endpoint + key`。规范化 scope/key、最终 `content_hash` 与发布状态持久化在 PostgreSQL `workspace_files` reservation 中，不依赖 API 进程内存；`workspace_files.status` 直接承担 `pending / ready / failed` 发布状态，不新增 Workspace reservation 表。幂等 metadata 保留 24 小时，Workspace File 本身继续按 Task 生命周期保存。新请求创建 reservation 并返回 `201`；同 key、同文件 hash 返回既有 `workspace_path` 和 `200`；同 key、不同 hash 返回 `409 idempotency_conflict`；reservation 尚未完成时返回 `409 idempotency_in_progress`。

#### 2.1.8 Document 原文读 / 下载

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/documents/{doc_id}` | 元数据投影（含 name、media_type、content_hash、status、tags） |
| `GET` | `/documents/{doc_id}/content` | 经 Document access 校验后读取原始对象，或返回短时签名 URL |

```text
GET /documents/{doc_id}/content
  → 按 auth_tenant.md §2.2 计算 Document access
  → 可选 ?disposition=inline|attachment
  → 受控代理字节流，或返回 { url, expires_at }（短时签名 URL，TTL ≤ 5 分钟）
```


`phase` 映射例（实现表可扩展，对外枚举保持稳定；属 §2.1.3 投影合同）：

```text
RouteNode → routing
Plan → planning
Deepen → deepening
Retrieval / Web|RAG|SQL → retrieving
MergeEvidence / AnalysisDecision / Compute / Coverage → analyzing
Critic → reviewing
LeadWriter → writing
Report Schema Validation / CitationValidation / Revise → reviewing
Finalize → completed
```

`phase=completed` 表示 Graph 已完成 Finalize；随后 TaskService 将 `ResearchTask.status` 更新为 `completed`。`phase` 与 `task_status` 分别表达 Graph 产品阶段和任务生命周期，不互相替代。

### 2.2 Chat / Thread

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/threads/{thread_id}` | 元数据 + 摘要 |
| `GET` | `/threads/{thread_id}/messages` | 分页对话回放 |
| `GET` | `/threads` | 对话历史列表（[`frontend.md`](../06_product/frontend.md)） |
| `POST` | `/threads` | 显式建会话（可选） |

消息不会通过独立 Chat 执行入口绕过 RouteNode。`POST /tasks` 原样持久化 user Message；direct/focused 完成后写 assistant Message，deep 完成后写报告可用通知并由 Task 投影暴露 `report.final`。


#### 2.2.1 Research LTM 管理

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/memories` | 当前用户记忆列表；支持 `status/category/topic` 过滤与 cursor 分页 |
| `POST` | `/memories` | 显式 remember；同步 gate/正文提交成功后返回 memory_id/revision/embedding_status |
| `PATCH` | `/memories/{memory_id}` | 用户确认的 manual edit；携带 expected_revision，同 topic 变化走 supersede |
| `POST` | `/memories/{memory_id}/resolve` | 解决 pending conflict：`accept_new` 或 `keep_old`；携带 pending/related 两方 expected_revision |
| `DELETE` | `/memories/{memory_id}` | forget；立即停止召回并在 24 小时内硬删除正文/embedding |


### 2.3 Events（SSE）

```text
GET /tasks/{task_id}/events
  → SSE；after_id / Last-Event-ID 可选，断线续传
```

每个持久化 Event 有稳定 **`event_id`**（单调游标）。  
废弃无鉴权 WS 作主推流。

### 2.4 Report Export（输出仍是 Workspace File）

Share / Export 路由合同在本文预定义，具体实现与验收属于 Phase 14；它们不属于 Phase 10 的交付范围。

| 方法 | 路径 | 作用 |
|------|------|------|
| `POST` | `/tasks/{task_id}/report/exports` | 为该 Task 的 final report 创建 docx/pdf ExportJob |
| `GET` | `/tasks/{task_id}/report/exports/{export_id}` | 状态、固定的 `report_version`、`workspace_path` |
| `GET` | `/tasks/{task_id}/report/exports/{export_id}/content` | 鉴权后下载 `reports/exports/**` 文件 |

创建 Export 时，API 在同一 PostgreSQL 事务中固定 `report_version`、写入 `export_jobs(status=pending)` 与 `outbox_messages(kind=report.export)`；`worker-export` 收到仅含 `export_id` 的消息后以 ExportJob CAS claim。
创建请求携带 `Idempotency-Key`；服务端对规范化的 `format + report_version` 计算 `request_hash`。同一 Task 下同 key、同 hash 返回既有 ExportJob，同 key、不同 hash 返回 `409 idempotency_conflict`。

### 2.5 产品附属资源

#### 2.5.1 Authentication

| 方法 | 路径 | 鉴权 | 作用 |
|------|------|------|------|
| `POST` | `/auth/login` | 无 Bearer | 校验账号与 tenant membership；签发 Access Token 与 Refresh Cookie |
| `POST` | `/auth/refresh` | Refresh Cookie | 定位并原子轮换 Session；返回新 Access Token |
| `POST` | `/auth/logout` | Refresh Cookie | 幂等撤销当前 Session、清 Cookie；204 |
| `POST` | `/auth/revoke` | Bearer | 撤销当前用户指定 Session 或全部 Session；204 |

Login body：

```json
{"email":"user@example.com","password":"...","tenant_id":"tenant_xxx"}
```

成功响应只返回 `access_token`、`token_type=Bearer`、`expires_in`、`session_id`、最小 user/tenant 投影与 `roles`；Refresh Credential 只通过 HttpOnly Cookie 传输。Refresh Credential 的 `session_id.secret` 格式、轮换、TTL 与撤销语义以 [`auth_security.md`](auth_security.md) 为准。

`POST /auth/revoke` body 二选一：

```json
{"session_id":"session_xxx"}
json
{"all_sessions":true}
```

#### 2.5.2 Document Upload / Ingestion

| 方法 | 路径 | 作用 |
|------|------|------|
| `POST` | `/documents` | `multipart/form-data` 单文件 streaming upload |
| `GET` | `/documents` | 当前 tenant 知识库的 Document 列表 |
| `GET` | `/documents/{doc_id}` | Document 元数据投影 |
| `GET` | `/documents/{doc_id}/ingestion` | Ingestion 当前状态与安全失败详情 |
| `POST` | `/documents/{doc_id}/ingestion/retry` | 对可重试失败重新排队；202 |
| `POST` | `/documents/{doc_id}/ingestion/rebuild` | 递增 generation 并重建索引；202 |
| `DELETE` | `/documents/{doc_id}` | 按 Document 删除/引用规则发起删除 |

上传请求：

```text
Content-Type: multipart/form-data
Idempotency-Key: <client-generated-key>     # 必填

file = 单个知识库文件                      # 必填；PDF / DOCX / TXT / MD / HTML
name = 显示名称                            # 可选，默认原文件名
```


新建成功：`201 Created`，并设置 `Location: /api/v1/documents/{doc_id}`。响应：

```json
{
  "doc_id":"doc_xxx",
  "name":"market-report.pdf",
  "media_type":"application/pdf",
  "size_bytes":1837294,
  "content_hash":"sha256:...",
  "status":"pending",
  "created_at":"...",
  "updated_at":"..."
}
```


Ingestion 状态：

```json
{
  "doc_id":"doc_xxx",
  "document_status":"failed",
  "published_generation":null,
  "ingestion":{
    "generation":1,
    "status":"failed",
    "stage":"parsing",
    "attempt_count":2,
    "error_code":"parse_failed",
    "updated_at":"..."
  },
  "updated_at":"..."
}
```

`retry` 复用当前 generation；`rebuild` 递增 generation。安全错误消息与可重试性由服务端根据 `error_code` 映射，详细异常只进入 Trace/日志。已有 `published_generation` 时，rebuild 期间和失败后 Document 继续保持 `ready`。


##### Upload Idempotency Contract

逻辑唯一范围：

```text
tenant_id + endpoint(POST:/api/v1/documents) + idempotency_key
  → doc_id + content_hash + expires_at
```

```text
新 key
  → 事务插入 document_upload_reservations（doc_id 已确定，content_hash=NULL）
  → streaming upload + 后端计算原始字节 SHA-256
  → Document finalize + reservation.content_hash
       + document_ingestions(generation=1,status=queued,attempt_count=0)
       + outbox_messages(kind=document.ingest,payload=doc_id) 同事务提交

已完成 key
  → 仍完整消费有界文件流并由后端计算 SHA-256
  → hash 不同：409 idempotency_conflict

key 正在处理（content_hash=NULL）
  → 409 idempotency_in_progress，retryable=true
```


产品界面的“知识库”使用 `GET /documents`，列出当前 tenant 的 Document，并通过 `tags`、`status` 和 Cursor 过滤。

#### 2.5.3 ModelConfig / DataSource / HITL / Share

```text
POST   /model-configs
GET    /model-configs
GET    /model-configs/{model_config_id}/versions/{version}
PATCH  /model-configs/{model_config_id}
POST   /model-configs/{model_config_id}/test
POST   /model-configs/{model_config_id}/disable
POST   /model-configs/{model_config_id}/versions/{version}/revoke

# DataSource（详见 ../02_capabilities/datasource.md）
POST   /datasources                         # admin；创建时 enabled=false
GET    /datasources                         # admin 全部；普通用户仅获授权最小投影（含派生 write_ready）
PATCH  /datasources/{datasource_id}         # admin；连接配置/credential rotate
POST   /datasources/{datasource_id}/enable
POST   /datasources/{datasource_id}/disable
PUT    /datasources/{datasource_id}/policies
PUT    /datasources/{datasource_id}/grants/{user_id}
DELETE /datasources/{datasource_id}/grants/{user_id}

# HITL（业务库写；详见 ../02_capabilities/database_write.md）
GET  /tasks/{task_id}/pending-writes/{write_id}
POST /tasks/{task_id}/pending-writes/{write_id}/approve
POST /tasks/{task_id}/pending-writes/{write_id}/reject
# Task：ResearchTask.status=paused + ResearchState.pending_write
# Share / Export（详见 [`share_export.md`](../06_product/share_export.md)）
POST   /shares                         # JWT；冻结 final report Snapshot，返回一次性 public URL
GET    /shares                         # 我的分享
DELETE /shares/{share_id}              # revoke
GET    /public/shares/{token}          # 匿名只读 Snapshot；无 JWT
GET    /public/shares/{token}/assets/{asset_id} # Snapshot manifest 内的只读 Asset

# Export 路由见 §2.4；产物为 Workspace reports/exports/** 文件
```

`POST /shares` body 为 `{ task_id, report_version?, title?, expires_at? }`；省略 `report_version` 时，服务端在创建流程中固定当前 `report.final.version`。Share 公开响应只解析冻结 Snapshot 与 manifest Asset。

### 2.6 公共错误响应

HTTP 错误统一采用 [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html)：

```http
Content-Type: application/problem+json
json
{
  "type":"urn:deepresearch:problem:invalid-argument",
  "title":"Invalid argument",
  "status":422,
  "code":"invalid_argument",
  "detail":"One or more request fields are invalid.",
  "instance":"/api/v1/documents",
  "request_id":"req_xxx",
  "retryable":false,
  "errors":[{"field":"file","code":"required","message":"A file is required."}]
}
```

- `request_id` 对应 Trace；`errors` 仅用于字段级校验。
- `413 upload_too_large`、`415 unsupported_media`、`409 invalid_state|idempotency_conflict`、`410 idempotency_target_deleted|export_expired`、`422 invalid_argument|invalid_cursor`、`429 rate_limited`（带 `Retry-After`）、`503 dependency_unavailable` 使用同一结构。

### 2.7 公共 Cursor 分页

所有 HTTP 列表统一：

```text
request:  limit=20（默认，最大 100） + cursor?（opaque）
response: { items: [...], page: { limit, has_more, next_cursor? } }
```

```text
token = base64url(payload) + "." + base64url(HMAC-SHA256(payload, CURSOR_SIGNING_KEY))
payload = { resource, scope_hash, filter_hash, sort, last_key, exp }
CURSOR_TTL_SECONDS = 1800
```


---

## 3. `task_status` ↔ API

本节的 `task_status` 是 API 字段名，唯一读取和更新 `ResearchTask.status`；ResearchState 不保存该字段。


### 3.1 对 UI

| task_status | UI 语义 | 控制 API |
|--------|---------|----------|
| `pending` | 已创建，排队 | cancel |
| `running` | 执行中（含深潜/修订） | cancel、pause、**replan** |
| `pausing` | 暂停已受理，等待安全 checkpoint | cancel、幂等 pause |
| `paused` | 已在 checkpoint 停稳，可 `/resume` | resume、cancel |
| `cancelling` | 取消收尾中 | 幂等 cancel |
| `cancelled` | 终态 | 只读 |
| `completed` | 终态已交付（含降级） | 只读 |
| `failed` | 终态未交付 | 只读；同 thread 可新建 task |

```text
terminal = cancelled | completed | failed
```

```text
修订中     → running + phase=reviewing
深潜中     → running + phase=deepening|retrieving
降级完成   → completed + coverage.stop_reason / reliability.degraded_branches
```

```text
paused = 用户协作暂停或等待业务写审批，保留可恢复的 Graph checkpoint
等待业务写审批 = paused + pending_write
核心节点 retry/fallback 耗尽 = failed + reliability.errors[].code=retry_exhausted
```

### 3.2 合法迁移

所有改变 Task/PendingWrite 状态的命令都要求 `Idempotency-Key`：`pause|resume|cancel|replan|approve|reject`。服务端规范化 `{command_type, path ids, body}` 后计算 `request_hash`，并在 `research_task_commands` 中持久化；同 key+同 hash 回放原接受结果，同 key+异 hash 返回 `409 idempotency_conflict`。

```text
命令事务（以 replan 为例）：
  reserve (tenant_id,user_id,task_id,idempotency_key,request_hash)
  → 校验 ResearchTask.status
  → 写一次 user Message + ResearchState replan trigger
  → 写 accepted_http_status + 有界 response_json + Event/Outbox Message
  → commit

命令记录保留到 Task 删除清理；PendingWrite approve/reject 还必须受自身状态 CAS 保护。
```

Pause 与 Cancel 提交 PG command、推进 `ResearchTask.status` 并写 Event，由当前 Worker 协作收敛；Resume 在同一事务中追加 `outbox_messages(kind=research.schedule)`，用于唤醒新的 Research Worker。

```text
POST /tasks
  → pending
  → research.schedule
  → Worker 成功 claim lease
  → running

POST /pause（协作）
  → 立即：running → pausing
  → Graph 到安全范围 + checkpoint 后：ResearchTask.status=paused

POST /resume
  paused + pending_write == null
    → pending + research.schedule Outbox Message(task_id)
    → Worker claim 后进入 running
    → 按 checkpoint graph position 续跑

  paused + pending_write != null
    → 409 invalid_state
    → 必须先 approve / reject，由 WriteGate 对应流程继续

POST /cancel
  pending|running|pausing|paused → cancelling → cancelled
  → cancellation_token → 停新节点、取消 in-flight Tool、cleanup
  terminal → 409 或幂等返回当前态

Graph Finalize 成功
  → TaskService 将 ResearchTask.status 从 running 更新为 completed

核心节点 retry/fallback 耗尽
  running → failed，错误码进入 reliability.errors[]

单 branch 失败 / tool retry 耗尽
  → branch failed + reliability
  → Graph outcome 由 Coverage / Finalize 决定
  → TaskService 据此更新 ResearchTask.status

```

### 3.3 HTTP 状态码

| 情况 | HTTP |
|------|------|
| `POST /tasks` Task 已持久化 | **201 Created**（task_status 可为 pending/running） |
| `POST /tasks` 幂等 replay 命中原 Task | **200 OK** |
| `DELETE /tasks/{task_id}` 已受理 | **202 Accepted** |
| pause / resume / cancel / replan / hitl 命令已受理 | **202 Accepted** |
| 上传新 Document 已持久化且 ingestion 已入 outbox | **201 Created** |
| Task data-file 已发布为 Workspace File | **201 Created** |
| Ingestion retry 已受理 | **202 Accepted** |
| 上传幂等 replay 命中原 Document | **200 OK** |
| 非法迁移、资源状态冲突、幂等冲突 | **409 Conflict** |
| 幂等 Key 指向已删除 Task、已过期 Export 内容 | **410 Gone** |
| ownership/effective access 不匹配 | **404 Not Found** |
| 未认证或 Session 无效 | **401 Unauthorized** |
| 参数/schema/cursor 失败 | **422 Unprocessable Content** |
| 上传原始字节超限 | **413 Content Too Large** |

错误 body 统一遵守 §2.6 的 error envelope。

---

## 4. SSE 事件合同（对 UI）

持久化业务 Event 先入 events/outbox，再推送；每条含稳定 **`event_id`**（推荐 SSE `id:` 字段；`after_id` / `Last-Event-ID` 续传）。

保留与背压合同：

```text
TASK_EVENT_RETENTION_DAYS=7          # terminal 后；Task 主动删除则按 24h 清理合同提前删除
SSE_HEARTBEAT_SECONDS=15
SSE_CONNECTIONS_PER_TASK_MAX=5
SSE_CONNECTIONS_PER_USER_MAX=20
SSE_CLIENT_BUFFER_EVENTS_MAX=256
SSE_CLIENT_BUFFER_BYTES_MAX=1048576

连接时 cursor < 当前最早 retained event_id
  → 409 event_cursor_expired

live 慢消费者超过任一 buffer 上限
  → best-effort 发 resync_required 后断开
```

Redis 只承载 live fan-out；任何 replay、cursor expiry 判定与关键事件恢复均以 PG `research_events` 为准。

`task_updated` 与 `export_updated` 是持久化业务 Event，写入 `research_events` 并使用稳定 `event_id`。`resync_required` 是非持久化 SSE transport control event，不写 `research_events`，也不占用稳定业务 Event 类型集合；heartbeat 使用 SSE comment/control frame。

SSE 是已提交状态的变更通知，不复制完整 Task、Evidence、Report 或 Workspace 内容。稳定事件收敛为：

```text
id: {event_id}
event: task_updated
data: {
  event_id,
  task_id,
  changed[],             # task_status | phase | route | messages | evidence | knowledge_graph | report | pending_write
  at
}

id: {event_id}
event: export_updated
data: { event_id, task_id, export_id, status, at }
```

`changed=["task_status"]` 表示 `ResearchTask.status` 已提交变化；SSE 合同不因内部字段所有权调整而改变。

收到 `task_updated` 后，客户端按 `changed[]` 读取 `GET /tasks/{id}`、Evidence、KG 或 Workspace。PendingWrite 引用和报告引用从 Task 投影读取；`export_updated` 后读取 ExportJob。

```text
UI：
  task_updated → 刷新 Task 投影并驱动 Progress
  changed 包含 messages → 刷新 Thread messages
  changed 包含 evidence → 刷新 Evidence 列表
  task_status=paused 且 pending_write 非空 → 获取审批 Preview
  completed → 按 report.final.workspace_path 读取报告
  export_updated → 获取 ExportJob
断线：after_id / Last-Event-ID 补洞
```

---

## 5. Service 层职责

```text
TaskService.create
  → Ownership
  → ensure Thread
  → 持久化 user Message(request.query) 原文
  → 本次输入 + 同 Thread 近期对话 → 补全完整 query
  → ResearchTask(status=pending)
  → ResearchState(query=完整查询) + checkpoint₀
  → research.schedule Outbox
  → 201 + task_id（及 message_id）

TaskService.pause
  → 校验 running
  → ResearchTask.status=pausing（202）
  → Graph 安全范围 + checkpoint → ResearchTask.status=paused

TaskService.resume
  → 校验 paused
  → ResearchTask.status=pending + research.schedule Outbox
  → Worker 抢 lease；按 checkpoint graph position 续跑

TaskService.cancel
  → ResearchTask.status=cancelling
  → cooperative abort/cleanup
  → ResearchTask.status=cancelled

TaskService.replan  → §6

TaskService.get

EventService.subscribe
  → ownership；从 after_id 推已落库事件
```


---

## 6. Replan（单一业务接口）

```text
POST /tasks/{task_id}/replan
{
  "instruction": "改成只看全球市场"
}
```

```text
→ Ownership
→ 仅 ResearchTask.status=running 接受（paused → 409，先 resume；终态 → 新建 Task）
→ 在安全中断点，用「本次 instruction + 该 message 之前的同 Thread 近期对话」补全完整查询
→ 安全中断点重新进入 RouteNode
→ RouteNode 覆盖 route
→ route=deep 时 ChiefArchitect 生成新 plan；direct/focused 时清空 active plan 并按对应路径执行
→ 保留 research_rounds / checkpoints / audit
```


与 [`graph.md`](../01_architecture/graph.md) §4 / [`memory.md`](../02_capabilities/memory.md) 一致。

---

## 7. 实现范围

```text
✓ Task / Thread / Event + Document / Evidence / Workspace File 等产品资源
✓ ResearchTask.status 八态与控制 API / 内部迁移范围一致；
  resume 按 checkpoint graph position 恢复
✓ POST /tasks 原子落 Message/Task/checkpoint₀/outbox；Replan 单接口
✓ “知识库”产品页直接使用 Document API
✓ Share / Export 只预定义公共路由合同；实现与验收留在 Phase 14

```

---

## 8. 验收标准

```text
□ POST /tasks → 201；原子 persist Message + Task + State + checkpoint₀ + research.schedule Outbox Message
□ `/workspace` 浏览当前 Task 文件，`/workspace/{path}` 统一读取文本与二进制
□ `/tasks/{task_id}/data-files` 仅接受 XLSX/CSV/Parquet，并发布为当前 Task 的 Workspace File
□ Evidence 列表/详情保持短结果内联、长结果引用；Task KG 查询返回 evidence_id provenance
□ cancel/pause/resume 合法；pause/cancel 通过 pausing/cancelling 状态协作收敛；非法 409
□ paused + pending_write 非空时普通 resume 返回 409，必须先 approve / reject
□ pause/resume/cancel/replan/approve/reject 均要求 Idempotency-Key；同 key 同 hash 回放，同 key 异 hash 返回 409
□ Phase 10 持久化 SSE 业务事件实现 task_updated，并使用稳定 event_id；支持 after_id / Last-Event-ID；export_updated 随 Phase 14 Export 实现
□ resync_required 与 heartbeat 只属于 SSE transport control，不写 research_events
□ terminal 事件默认保留 7 天；过旧 cursor 返回 409 event_cursor_expired，客户端按 snapshot + live 恢复
□ 普通聊天、focused 工具任务与 deep 研究统一走 POST /tasks，并由 RouteNode 分流
□ TaskProjection 组合 ResearchTask.status 与 ResearchState 业务字段；对外 task_status 只读取 ResearchTask.status
□ running 可通过 POST /tasks/{id}/replan 改目标；instruction 原文写入 Thread；
  同 Thread 近期对话补全并覆盖 ResearchState.query，RouteNode 重算 route，deep 时 ChiefArchitect 再覆盖 plan；历史执行保留；paused 首期 409
□ 创建与 Replan 统一覆盖现有 ResearchState.query，conversation_messages 保存原文
□ login/refresh/logout/revoke 路由闭环；Refresh Credential 仅 Cookie，Session 可定位且可撤销
□ Document 与 Task data-file 上传 Idempotency-Key 均有 PG reservation、24h TTL、hash 比较与并发冲突语义；data-file reservation 复用 workspace_files.status
□ 产品“知识库”通过 tenant 级 Document 列表完成浏览和授权
□ ResearchTask.status 八态与 Runtime / API 生命周期合同一致；branch 失败≠自动整 Task failed
□ 与 auth_tenant：路由先授权具体根资源，子资源继承父级授权
□ 业务写暂停：paused + pending_write（见 database_write.md）
```

---

## 9. 实现落点

> 全局依赖顺序见 [`docs/README.md`](../README.md)；以下为 API 模块内部落点。

1. FastAPI 路由骨架 + AuthContext + RFC 9457 exception handler + 签名 Cursor codec
2. login/refresh/logout/revoke；再开放其余受保护路由
3. Document streaming upload/idempotency + ingestion status/retry + tenant scope；Task data-file 流式发布到 Workspace
4. TaskService（create 含 Message/幂等事务；terminal delete）+ Task 投影
5. Evidence、Task KG 与统一 Workspace 文件读取接口
6. SSE task_updated 与前端刷新逻辑；export_updated 留到 Phase 14 Export 实现
7. Runtime API integration：pause / resume / cancel / replan 命令接入现有 Task Runtime；API 不重新实现 lease、checkpoint、cancellation 与 recovery（[`runtime.md`](../03_runtime/runtime.md)）

## 附录：最小测试集

```text
□ POST /tasks 新建 201；同 key 同 hash replay 200；异 hash 409；soft delete 后 replay 410
□ 两个并发同 key 请求只产生一个 Message/Task/checkpoint₀/outbox
□ 八态下 pause/resume/cancel/replan 的合法与非法转换
□ paused + pending_write 非空时 resume 返回 409，approve/reject 后只能由 WriteGate 对应流程继续
□ 六类 Task/PendingWrite 命令同 key/同 hash 幂等回放、异 hash 冲突，并发请求只提交一次状态/outbox
□ SSE Last-Event-ID 重连、PG replay、heartbeat、7 天 retention/cursor expiry、慢消费者背压与 Redis 丢失恢复
□ Evidence、KG 与 Workspace API 的 Ownership、范围和 path traversal 测试
□ Document/Task data-file 上传的 100/256 MiB 范围、MIME 欺骗、中途超限、同/异 hash 幂等 replay 与并发 reservation
□ Ingestion queued/processing/succeeded/failed、retry 同 generation、rebuild 新 generation 与 error_code 路由矩阵
□ Document tenant scope、列表过滤、RAG tenant filter 与跨租户 404
□ auth session 定位/轮换/replay/logout/revoke；非 membership tenant 登录/切换失败
□ RFC 9457 schema；Cursor 签名篡改、过期、跨 scope/filter replay 返回 invalid_cursor
```
