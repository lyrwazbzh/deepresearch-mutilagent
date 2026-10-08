# DeepResearch-mutilagent：部署（Deployment）

## 0. 定位

```text
单机 Docker Compose
  + HTTP 与长任务分离
  + PostgreSQL 权威状态
  + Outbox 驱动异步投递
  + Research / Ingestion / Export 三队列隔离
```

核心数据归属：

```text
PostgreSQL(+pgvector)
  = 业务状态、ResearchState、Checkpoint、Event、Outbox、研究记忆与文件元数据

MinIO
  = Document 原文件与 Research Workspace 文件

Redis
  = Celery Broker 与 SSE live fan-out

Elasticsearch(+IK)
  = Document Chunk 的 BM25 / dense_vector 派生检索索引

Neo4j（`task-kg` profile）
  = 当前 ResearchTask 的知识图谱派生索引；Phase 9 实现与验收 Task KG 时启用
```

Local 与 Production 使用相同组件类型；差异由地址、Secret、资源限制、镜像版本、TLS 和持久化配置表达。

Phase 9 包含 Task KG 的实现与验收。Neo4j 通过 `task-kg` profile 启用；未启用该 profile 时，仅 Task KG 能力不可用，API、Research 主链与其他核心进程的启动、readiness 和恢复流程不依赖 Neo4j。

---

## 1. 线上拓扑

```text
                              Internet
                                 │
                              HTTPS
                                 │
                              Nginx
                     ┌───────────┴───────────┐
                     ↓                       ↓
                  Frontend                  API
                                             │
                         ┌───────────────────┼───────────────────┐
                         ↓                   ↓                   ↓
                    PostgreSQL             MinIO          Redis live fan-out
                         │
                         │ committed outbox
                         ↓
                 outbox-dispatcher
                    ├─ Event Outbox ───────────→ Redis live fan-out ──→ API / SSE
                    └─ Job Outbox ─────────────→ Redis / Celery Broker
                                                   │
                         ┌─────────────────────────┼─────────────────────────┐
                         ↓                         ↓                         ↓
                  research queue             ingestion queue            export queue
                         ↓                         ↓                         ↓
                  worker-research           worker-ingestion          worker-export
                    ├─ PG / Checkpoint         ├─ PG Ingestion           ├─ PG ExportJob
                    ├─ Workspace / MinIO        ├─ MinIO Document         └─ Workspace / MinIO
                    ├─ ES                       ├─ ModelRuntime.embed ──→ External Model Provider
                    ├─ Neo4j（Task KG profile）
                    ├─ ModelRuntime ───────────→ Model Provider（托管 API 或私有推理服务）
                    ├─ ToolRuntime ────────────→ External Tool Provider
                    ├─ RetrievalGateway
                    └─ DockerSandboxRunner ────→ Docker Engine + runsc
                                                   └─ 单次执行的临时 Sandbox Container
```

部署边界：

- `ModelRuntime` 是 API、Research、Ingestion 和 RAG 共用的模型调用模块；它不单独部署。
- `RetrievalGateway` 是 Research Worker 内的 Document RAG 编排模块；它不单独部署。
- `DockerSandboxRunner` 是 Research Worker 内的 Sandbox Runtime Adapter，不是独立常驻服务；它通过 Docker Engine + `runsc` 为每次 AnalysisRequest 创建并销毁临时隔离容器。
- Neo4j 通过 `task-kg` profile 部署；Phase 9 的 Task KG 实现与验收启用该 profile，但 Neo4j 不作为 Research 主链的硬 readiness 依赖。
- API 在 PostgreSQL 提交业务记录与 Outbox 后返回；Job 由 `outbox-dispatcher` 发布。

长任务路径：

```text
POST /tasks
  → TX{ ResearchTask + State + checkpoint₀ + scheduling_outbox }
  → 201 + task_id
  → outbox-dispatcher
  → research queue
  → worker-research claim Task lease
  → LangGraph 执行并持续提交 State / Checkpoint / Event / Outbox
```

---

## 2. Compose 服务与镜像

```text
nginx
frontend
api
outbox-dispatcher
worker-research          # Celery -Q research
worker-ingestion         # Celery -Q ingestion
worker-export            # Celery -Q export

# 可选 private-inference profile
model-server-chat
model-server-embedding
model-server-rerank

# task-kg profile；Phase 9 Task KG 实现与验收时启用
neo4j

postgres                 # PostgreSQL + pgvector
redis                    # Celery Broker + SSE live fan-out
minio                    # documents / workspaces 两个私有 Bucket
elasticsearch            # IK + BM25 + dense_vector
```

应用镜像使用固定版本标签，并在生产 Compose 中固定 digest。一个 Dockerfile 提供按依赖拆分的构建目标：

```text
backend-core:<version>   → api / outbox-dispatcher / worker-research
ingestion:<version>      → worker-ingestion；包含 Parser / OCR / MinerU 依赖
export:<version>         → worker-export；包含 Pandoc / Chromium 依赖
sandbox:<version>        → DockerSandboxRunner 按调用启动的一次性执行镜像；不是 Compose 常驻服务
frontend:<version>       → 静态前端
```

基础设施镜像同样固定具体版本。开发环境可使用源码 bind mount；生产环境只运行构建产物。

---

## 3. API 与 Outbox

API 负责短请求：

```text
Auth / Thread / Message
ResearchTask 创建与控制命令
Document 上传与 Ingestion 控制
Evidence / Workspace / Report 查询
Share / Export
SSE 连接与 PG Event replay
Health
```

创建异步工作时，API 在同一 PostgreSQL 事务中提交业务记录和 Outbox：

```text
业务事务 commit
  → outbox-dispatcher 发布 Job
  → Worker 依据 PG 领域状态 claim
```

API 使用受信代理配置解析 `X-Forwarded-*`。SSE live event 从 Redis 接收，断线回放以 PostgreSQL `research_events` 为准。

---

## 4. 三条核心业务队列

| Queue | Payload | PostgreSQL claim | 负载特征 | Celery 策略 |
|---|---|---|---|---|
| `research` | `task_id` | ResearchTask lease | 长链路 Agent、较多外部 I/O | 低并发、prefetch 1、late ack |
| `ingestion` | `doc_id` | `document_ingestions` CAS | Parser/OCR/Embedding，CPU 与内存较重 | 资源受限并发、prefetch 1、late ack |
| `export` | `export_id` | ExportJob CAS | Pandoc/Chromium 转换 | 有界并发、prefetch 1、late ack |

统一规则：

```text
Celery task_id   = 投递与观测 ID
业务 ID          = task_id | doc_id | export_id
业务状态与结果    = PostgreSQL
Celery result    = ignore；不作为业务状态来源
```

`task_id`、`doc_id` 与 `export_id` 是全局唯一的 opaque ID。内部 Worker 可据此读取资源记录并重建 tenant/user scope；公共 API 仍必须在已认证 scope 内查询资源。

Worker 并发数通过环境配置按机器资源确定。消息未 ACK 时允许 Broker redelivery；重复交付由领域 claim 或 CAS 消解。

Celery 负责粗粒度 Job 调度；ResearchTask 内部的 Web、RAG、SQL 并行由 LangGraph 与 asyncio 编排，Compute 仅在 MergeEvidence 后按 AnalysisRequest 调度。

Task KG 的异步派生构建复用 `research` queue，由 `worker-research` 执行：

```text
MergeEvidence commit
  → knowledge_graph.build Outbox
  → research queue（payload = task_id）
  → worker-research
  → 读取当前 Query、Plan 与 Evidence
  → 更新 Neo4j 的 tenant_id + task_id namespace
```

Task KG Build 是 Graph / Runtime 内部派生 Job，不新增独立 KG queue 或常驻 `worker-kg`。

---

## 5. Research Worker

Research Job 只携带 `task_id`：

```text
run_research(task_id)
  → 从 PG 读取 ResearchTask
  → 重建 tenant_id / user_id / thread_id
  → claim lease_token
  → 读取 ResearchState 与最近 checkpoint
  → 恢复 LangGraph position
  → 执行 pending NodeRun
```

节点提交：

```text
StateManager.commit_node(
  node_run_id,
  patch,
  expected_lease_token
)
```

同一 PostgreSQL 事务完成：

```text
锁定 ResearchTask 行
  → lease fencing
  → 校验 NodeRun 与 patch 字段所有权
  → ResearchState patch 合入 latest_state
  → checkpoint
  → research_event
  → outbox_messages(kind=research.event.publish)
  → NodeRun completed
```

Worker 崩溃后，Task lease 到期。Recovery Scanner 只将 `running` Task 转回 `pending` 并写入 `outbox_messages(kind=research.schedule)`；新 Worker 使用新 `lease_token` 从最近成功 checkpoint 继续。`pausing` 收敛为 `paused`，`cancelling` 在清理残留资源后收敛为 `cancelled`，两者不重新调度研究执行。

### 5.1 Pause / Resume / Cancel

```text
Pause
  → API 写 PG pause command + task_status=pausing
  → 当前 Worker 在节点或长调用安全边界读取
  → commit checkpoint
  → task_status=paused 并释放 lease

Resume
  → API 校验 paused
  → PG 写 resume command + task_status=pending + outbox_messages(kind=research.schedule)
  → Worker 新 claim
  → 从 checkpoint 恢复

Cancel
  → API 写 PG cancel command + task_status=cancelling
  → 当前 Worker 停止启动新节点
  → cancellation 传播到 Tool、ModelRuntime 与 Sandbox
  → 清理完成后 task_status=cancelled
```

Pause 和 Cancel 由当前执行进程协作收敛；Resume 通过 Outbox 重新唤醒 Research Worker。Celery revoke 作为执行进程失控时的运维兜底。

---

## 6. Outbox、投递与恢复

三个逻辑层分别负责：

```text
Outbox Publisher
  = 发布已经提交的 Job / Event

Celery
  = at-least-once 投递与未 ACK 消息 redelivery

Domain Recovery Scanner
  = 按 PostgreSQL 领域状态恢复未完成工作
```

部署上使用一个 `outbox-dispatcher` 进程，内部运行两个独立循环：

```text
Publisher Loop
  → 扫描未 dispatched Outbox
  → Job 发布到 Celery Broker
  → Event 发布到 Redis live fan-out
  → 标记 dispatched

Recovery Loop
  → 扫描异常领域状态
  → 在 PG 写入恢复所需 Outbox
  → 由 Publisher Loop 发布
```

稳定身份与去重：

```text
message_id     → Outbox Message 投递身份
event_id       → SSE replay 与客户端去重
领域 claim     → ResearchTask / Document Ingestion / ExportJob 消费去重
Celery task_id → 单次 Broker 投递观测
```

三类重试：

```text
Outbox 重投       → 记录尚未可靠交给 Broker
Celery redelivery → 已交付消息未 ACK
领域 retry        → 领域状态回到可调度态，并提交新的 Outbox
                    ResearchTask=pending
                    Document Ingestion=queued
                    ExportJob=pending
```

Recovery Loop 只修复调度事实：ResearchTask 的过期 lease、Ingestion 的 queued/超时 processing、ExportJob 的 pending/超时 running 分别经 CAS 回到对应可调度态并写入新 Outbox。Export Worker 每次 claim 递增 `attempt_count`，运行期间按当前 attempt 刷新 `updated_at`，最终提交匹配当前 attempt；Graph、Parser 和 Renderer 仍由对应 Worker 执行。

Redis 中断后，SSE 使用 `Last-Event-ID` 从 PG 补齐历史，再切回 Redis live fan-out。

---

## 7. Document Ingestion

### 7.1 当前处理控制记录

每个 Document 使用一条当前 Ingestion 控制记录：

```text
document_ingestions
  tenant_id
  doc_id
  generation
  status                 # queued | processing | succeeded | failed
  stage                  # parsing | chunking | embedding | indexing | publishing
  attempt_count
  error_code?
  updated_at

PRIMARY KEY (tenant_id, doc_id)
```

```text
generation = 1       → 首次处理
generation 不变重跑  → retry
generation += 1      → rebuild
```

错误详情与堆栈进入 Trace/日志；API 根据 `error_code` 映射安全消息和可重试策略。

### 7.2 上传与 Worker claim

```text
POST /documents
  → PG 创建 document_upload_reservations(tenant_id,idempotency_key,doc_id)
  → 流式写入 MinIO 临时对象
  → 计算 size / content_hash / media_type
  → 发布 Document 原始对象
  → PG TX{
       finalize Document,
       finalize reservation.content_hash,
       initialize document_ingestions(generation=1,status=queued,attempt_count=0),
       outbox_messages(kind=document.ingest, payload=doc_id)
     }
  → 201
```

Celery 消息保持最小 payload：

```text
ingest_document.delay(doc_id)
```

Worker claim：

```text
读取 document_ingestions WHERE doc_id=?
  → status != queued：结束本次投递
  → CAS queued → processing，同时 attempt_count += 1
  → 保存 expected_generation + expected_attempt_count
```

阶段更新与最终发布均校验：

```text
doc_id
generation = expected_generation
attempt_count = expected_attempt_count
status = processing
```

跨存储失败按权威状态补偿：原始对象已发布而 PG finalize 失败时先 best-effort 删除，残留对象由 orphan GC 清理；PG 已提交但对象不可读时 Ingestion 以稳定 `error_code` 失败。ES 的部分 generation 可以被同一 generation/attempt 幂等覆盖或后台清理，在 `published_generation` 切换前不会进入检索。

### 7.3 处理与原子可见性切换

```text
MinIO 原始文件
  → Scanner / Parser / OCR
  → Normalize
  → Parent / Child Chunk
  → ModelRuntime.embed
  → 新 generation 写入 Elasticsearch
  → refresh 并确认新 generation 可读取
  → 发布 CAS
```

Document 保存：

```text
status                  # pending | ready | failed
published_generation?   # 当前 RAG 可见 generation
```

发布成功时，PostgreSQL CAS 更新 `documents.published_generation` 并将 Ingestion 标记为 `succeeded`。RetrievalGateway 的 BM25 与 kNN 都使用当前 `published_generation` 过滤候选。

这是以 PG 指针控制的原子可见性切换，不是 PostgreSQL 与 Elasticsearch 的分布式事务：

- ES 写入完成而 PG 尚未切换时，新 generation 保持不可见。
- 首次处理失败且没有可用索引时，Document 为 `failed`。
- 已有索引的 rebuild 期间，Document 保持 `ready` 并继续使用旧 generation。
- rebuild 失败不改变旧 `published_generation`。
- 旧 generation 的物理清理由 Document/Citation 生命周期处理。

### 7.4 Retry、Rebuild 与恢复

```text
Retry
  → generation 不变
  → status=queued
  → 下一次 claim 时 attempt_count++

Rebuild
  → generation++
  → status=queued
  → attempt_count=0
  → error_code=null
```

Recovery Scanner：

```text
queued
  → 确保存在可发布的 document.ingest Outbox Message

processing 且 updated_at 超过执行超时
  → CAS processing → queued
  → 写入恢复 Outbox
```

Worker 的阶段提交或 heartbeat 刷新 `updated_at`。重建后的旧 Worker 因 generation 不匹配无法提交；恢复后的旧 attempt 因 attempt_count 或 status 不匹配无法提交。

---

## 8. Workspace、MinIO 与 Sandbox

MinIO 使用两个私有 Bucket：

```text
documents
  {tenant_id}/{doc_id}/source/**
  {tenant_id}/{doc_id}/tmp/**

workspaces
  {tenant_id}/{task_id}/files/**
  {tenant_id}/{task_id}/visualizations/**
  {tenant_id}/{task_id}/reports/**
  {tenant_id}/{task_id}/reports/exports/**
  {tenant_id}/{task_id}/reports/shares/**
  {tenant_id}/{task_id}/tmp/**
```

PostgreSQL 保存 `storage_ref / content_hash / media_type / size / ownership / status`；对象正文保存在 MinIO。`storage_ref` 是 Repository 内部物理引用，API 与 ResearchState 使用 `doc_id` 或 `workspace_path`。

文件发布：

```text
写临时对象
  → 校验 hash / size / media_type / quota
  → 提升为最终 key
  → 提交 PG 文件元数据
```

普通读取由 API 受控代理；大文件可在授权后返回短时签名 URL。Bucket 始终保持私有。

Sandbox 边界：

```text
Research Worker 准备只读输入
  → Worker 内的 DockerSandboxRunner
  → Docker Engine + runsc 为单次 AnalysisRequest 创建临时隔离容器
     （容器内无 PG / MinIO / Redis / ES / Secret 凭据）
  → 只读挂载代码与输入，唯一可写位置为受限输出目录
  → 返回受控输出目录与执行诊断
  → Worker 校验输出
  → WorkspaceService 发布到 workspaces Bucket
  → 销毁本次临时容器
```

P0 的 `DockerSandboxRunner` 使用 Linux `gVisor/runsc` 创建隔离容器，并同时施加只读 rootfs、默认无网络、非 root 用户、无宿主目录写挂载、seccomp/capability 收缩、CPU/内存/PID/磁盘/时限。确需联网的任务只能使用显式允许的受控 egress proxy，Sandbox 内仍不注入业务存储或 Provider Secret。Firecracker microVM 作为需要更强租户隔离时的后续 backend；两者实现同一 Sandbox Runtime 合同，不改变 CodeWizard、Workspace 或 Evidence。

---

## 9. Elasticsearch 与 Retrieval

Elasticsearch `document_chunks` 保存：

```text
Chunk text
IK / BM25 字段
dense_vector
tenant_id
doc_id / chunk_id / generation
Parent / Child identity
```

P0 检索路径：

```text
BM25 Top-K + kNN Top-K
  → 应用层 RRF
  → ModelRuntime Reranker
  → Parent Expansion
```

- 应用层 RRF 是 P0 默认。
- Elasticsearch 原生 RRF 仅在部署版本与 License capability 满足时启用。
- 原生与应用层 RRF 返回相同 RetrievalResult 合同。
- Reranker 与 Parent Expansion 由 RetrievalGateway 执行。
- BM25 与 kNN 在召回前使用相同 `tenant_id` 与 `published_generation`。
- ES 是可重建的派生检索索引；Document 引用仍使用 `doc_id + chunk_id + content_hash`。

---

## 10. 模型调用与外部 Provider

```text
ModelConfig
  → ModelResolver(binding_key, purpose)
  → 固定 Model Binding

Agent / Ingestion / RAG
  → ModelRuntime(binding)
  → Provider Client
  → Model Provider
```

```text
ModelResolver
  = 选择并固定 revision，不调用 Provider

ModelRuntime
  = revision 撤销检查
  + Secret 按需解析
  + timeout / retry / Circuit Breaker
  + 固定 fallback chain
  + cancellation / deadline
  + 错误归一化
  + Token / Cost / Trace

Provider Client
  = Provider 协议转换
```

API 仅在 ModelConfig 测试时执行最小真实请求；Research Worker 使用 Chat / Rerank，Ingestion Worker 使用 Embedding。Task 创建时按角色 binding 固定 primary 与 fallback；多个角色可以复用同一个 ModelConfig。

私有模型服务使用 OpenAI-compatible endpoint 接入，主栈只保存 `base_url + secret_ref`：

```text
P0 serving engine   → vLLM
可选替换            → SGLang（同一 Provider Client 合同）

精度优先            → BF16 / FP16
硬件支持且已验证    → FP8
显存受限            → AWQ / GPTQ
```

量化不是全局开关，而是精确 Model revision 的部署画像。每个私有 endpoint 固定：

```yaml
model_deployment_profile:
  model: string
  revision: string
  serving_engine: vllm | sglang
  dtype: bf16 | fp16 | fp8
  quantization: null | awq | gptq
  gpu_arch: string
  tensor_parallel_size: int
  max_model_len: int
  max_num_seqs: int
  gpu_memory_utilization: float
```

角色绑定为：`routing → RouteNode`、`reasoning → ChiefArchitect/CriticMaster`、`tool → DeepScout`、`data → DataAnalyst`、`code → CodeWizard`、`writing → LeadWriter`，Embedding 与 Rerank 使用组件 binding。绑定允许按部署需要复用或拆分 endpoint，不要求每个 Agent 独占模型。部署前以目标上下文长度和并发压测验证显存、KV Cache、吞吐和延迟，不用模型参数量直接推断可承载并发。

---

## 11. 备份与恢复

### 11.1 数据分类

| 数据层 | 角色 | 恢复策略 |
|---|---|---|
| PostgreSQL(+pgvector) | 业务状态、对话、研究记忆、Checkpoint、Outbox、元数据 | 权威备份 |
| MinIO | Document 原文件与 Workspace 文件 | 权威备份 |
| KMS / Secret 恢复材料 | 解密 Provider/DataSource 凭据 | 权威备份 |
| Elasticsearch | Document Chunk 检索副本 | Snapshot 加速或由 PG+MinIO 重建 |
| Neo4j（`task-kg` profile） | Task KG 派生索引 | 启用该 profile 时由 Task Evidence 重建 |
| Redis | Broker 与 live fan-out | 由 Outbox、PG Event 和领域状态恢复 |

### 11.2 备份与恢复顺序

```text
PostgreSQL
  → 定期 base backup + WAL 归档

MinIO
  → 增量备份 + 对象清单与 checksum 校验

KMS / Secret
  → 独立备份密钥材料与恢复配置

Elasticsearch
  → 可选 Snapshot；定期演练从 Document 原文件、PG 元数据及固定的解析/Embedding 配置重建
```

恢复顺序：

```text
1. KMS / Secret
2. PostgreSQL
3. MinIO
4. 启动空的 Redis Broker / live 通道；不恢复旧队列或 live 消息
5. API / outbox-dispatcher / Workers
6. Elasticsearch 恢复或重建
7. 若启用 `task-kg` profile，Neo4j Task KG 按需重建
8. 由 Outbox、PG Event 与领域状态重建投递和 live 流
```

恢复演练校验 PG/MinIO 引用一致性、缺失对象、orphan 对象、Secret 解密、Outbox 重扫、Document 索引和 Workspace 文件完整性。

P0 目标：

```text
系统 RPO                         ≤ 6 h
Core data recovery（PG+MinIO）  ≤ 8 h
Complete application recovery   ≤ 24 h
```

---

## 12. Nginx

Nginx 是唯一公网入口。PG、Redis、MinIO、ES 与可选 Neo4j 仅在内部网络开放；一次性 Sandbox Container 不发布对外端口。

路由按流量类型拆分：

```nginx
location ~ ^/api/v1/tasks/[^/]+/events$ {
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}

location ~ ^/api/v1/tasks/[^/]+/data-files$ {
    client_max_body_size 260m;   # multipart 开销；应用层精确限制原文件为 256 MiB
    proxy_request_buffering off;
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /api/v1/tasks/ {
    client_max_body_size 2m;
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location = /api/v1/documents {
    client_max_body_size 105m;
    proxy_request_buffering off;
    proxy_pass http://api:8000;
}

location /api/ {
    client_max_body_size 2m;
    proxy_pass http://api:8000;
}
```

实际配置将 SSE 与 Task data-file location 放在普通 Task API 前，并复用统一的 proxy headers include。`POST /api/v1/tasks/{task_id}/data-files` 接收由 SQL/Data 分支处理的 XLSX、CSV、Parquet，不属于知识库 Document。Nginx 为 multipart 开销预留 `260m`，应用层对原始文件执行精确的 `256 MiB` 上限；该限制不扩散到普通 `/api/`。Document 上传继续使用独立路由、大小限制和流式校验。

---

## 13. Readiness、资源与迁移

### 13.1 Health

```text
API liveness              → 进程事件循环可响应
API readiness             → PostgreSQL + schema migration 状态
outbox-dispatcher ready   → PostgreSQL + Redis Broker
worker-research ready     → PostgreSQL + Redis Broker
worker-ingestion ready    → PostgreSQL + Redis Broker + MinIO + Elasticsearch
worker-export ready       → PostgreSQL + Redis Broker + MinIO
Sandbox backend preflight → 在 worker-research 部署检查中验证 Docker Engine + runsc 可创建受限临时容器；无独立 readiness endpoint
private model server ready → 已配置的模型 revision、量化格式与 GPU 可加载
```

模型 Provider 与 Reranker 等可降级依赖由运行时健康、Circuit Breaker 和 fallback 管理，不作为 API readiness 的硬条件。可选 Neo4j 未启用时不参与任何核心进程 readiness；启用 `task-kg` profile 后只影响 Task KG 能力自身的健康状态。

### 13.2 资源基线

```text
Research Worker   → 按并发模型调用与上下文内存配置
Ingestion Worker  → 按 Parser/OCR、Chunk、Embedding 批处理配置
Export Worker     → 按 Chromium/Pandoc 峰值内存配置
Sandbox Container → 由 DockerSandboxRunner 为每次执行施加独立 CPU、内存、磁盘、进程数和执行时间限额
Private inference → 按 model deployment profile 独占或切分 GPU；容量由目标上下文与并发压测确定
```

演示与少量并发、模型走外部 API 时，可从 `8 vCPU / 16–32 GiB RAM / SSD` 起步，再按指标调整 Worker 并发与资源限制。

### 13.3 Migration

```text
1. 独立 migration Job 执行 alembic upgrade head
2. migration 成功后启动或滚动更新 API / Workers
3. readiness 校验 schema revision
```

迁移镜像导入全部 ORM metadata，CI 校验 Alembic autogenerate 不遗漏模型。

---

## 14. Local、Production 与 CI/CD

```text
compose.yaml
compose.production.yaml
```

| | Local | Production |
|---|---|---|
| 组件类型 | PG/Redis/MinIO/ES + 应用服务；Neo4j 为可选 `task-kg` profile | 相同 |
| 镜像 | 固定开发版本 | 固定版本 + digest |
| TLS | 可选 | Nginx HTTPS |
| Volume | 本地持久卷 | 备份覆盖的持久化磁盘 |
| 源码 mount | 可选 | 构建产物 |
| restart | 按需 | 明确 restart policy |
| 资源限制 | 开发配置 | 按服务独立限制 |

CI/CD：

```text
testing.md 的 Pull Request 工程门禁通过
  → build 多目标镜像
  → 生成并固定 digest
  → migration compatibility check
  → Compose smoke test
  → 部署 migration Job
  → 滚动启动服务
  → readiness / queue / SSE / upload smoke test
```

生产演练包含 PG/MinIO 恢复、Outbox 恢复、ES 重建与 Worker 崩溃恢复。

---

## 15. 验收标准

```text
□ 公网只开放 Nginx 80/443；内部数据服务不直接暴露
□ 所有镜像固定版本，生产进一步固定 digest
□ 仅 research / ingestion / export 三条核心业务队列
□ 三条队列分别以 task_id / doc_id / export_id 为最小 payload
□ Celery result 不作为业务状态；重复交付由 PG claim/CAS 消解
□ Research Worker 可从 checkpoint 恢复；Node 提交受 lease_token fencing
□ Pause/Cancel 由当前 Worker 协作收敛；Resume 经 Outbox 重新调度
□ outbox-dispatcher 同时发布 Job/Event，Recovery Loop 只恢复领域调度事实
□ Ingestion 使用 generation + attempt_count 隔离重建、重试和迟到 Worker
□ Export 使用 attempt_count + updated_at 隔离 Recovery 后的迟到 Worker
□ Document rebuild 期间旧 published_generation 持续可检索
□ PG published_generation 控制检索可见性，不宣称 PG/ES 分布式原子事务
□ MinIO 只有 documents / workspaces 两个私有 Bucket
□ DockerSandboxRunner 是 Worker 内 Runtime Adapter，不作为 Compose 常驻服务
□ 临时 Sandbox Container 无 PG/MinIO/Redis/ES/Secret 凭据，也不发布对外端口
□ P0 Sandbox backend 为 gVisor/runsc；Firecracker 只作为同合同的更强隔离替换项
□ 私有模型服务固定 model revision、engine、dtype/quantization、GPU 与并发画像
□ P0 使用应用层 RRF；Rerank 与 Parent Expansion 位于 RetrievalGateway
□ PG、MinIO、KMS/Secret 可恢复；ES、Redis 有明确重建路径；Neo4j 仅在启用 `task-kg` profile 时按 Evidence 重建
□ 启用 task-kg profile 后，worker-research 可完成 Task KG 构建与查询；未启用时不影响 Research 主链 readiness
□ 普通 API、SSE、Document 上传与 Task data-file 上传使用独立 Nginx 策略
□ migration 单独执行；各进程 readiness 与职责匹配
```

---

## 16. 实现顺序

1. 固定版本的 Compose、多目标镜像与内部网络。
2. PostgreSQL Outbox、`outbox-dispatcher` 与三条 Celery 队列。
3. ResearchTask lease、checkpoint resume 与控制命令。
4. Document Ingestion 控制记录、generation 发布和 Recovery Loop。
5. 两个 MinIO Bucket、WorkspaceService、DockerSandboxRunner 与临时 Sandbox 输出发布。
6. Elasticsearch mapping、应用层 RRF、Rerank 与 Parent Expansion 接线。
7. Neo4j `task-kg` profile、Task KG Build / Query、Evidence provenance 与重建接线。
8. Nginx 分流、readiness、Migration Job 与 CI/CD。
9. 备份、恢复和崩溃演练。

## 附录：最小测试集

```text
□ API commit 后 outbox-dispatcher 崩溃，重启后 Outbox 仍能投递
□ Broker 重复交付时三类 Worker 只有一个领域 claim 生效
□ Research Worker 崩溃后从最近 checkpoint 恢复
□ Pause/Cancel/Resume 在 Worker 丢失与恢复场景下收敛到合法状态
□ Ingestion retry 复用 generation；rebuild 递增 generation
□ 旧 Ingestion Worker 无法覆盖新 generation 或新 attempt
□ ES 写入后 PG 切换前崩溃时，新 generation 不进入检索
□ rebuild 失败时旧 published_generation 继续提供 RAG
□ DockerSandboxRunner 不形成独立常驻服务；临时 Sandbox Container 无存储与 Secret 凭据，输出经 WorkspaceService 发布
□ Redis 清空后 Job 可由 Outbox/领域状态恢复，SSE 可由 PG Event replay
□ 启用 task-kg profile 后可从当前 Task Evidence 构建并查询 KG；Neo4j 清空后可由 Task Evidence 重建
□ Nginx 的普通 API、SSE、Document 上传与 Task data-file 上传限制分别生效
```
