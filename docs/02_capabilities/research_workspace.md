# DeepResearch-mutilagent：Research Workspace

## 0. 定位

Research Workspace 是当前 `ResearchTask` 的工作记忆文件系统。它负责保存 Web / SQL 的长内容、Compute 生成文件和报告产物，使 `ResearchState` 保持为短结构与引用。

```text
ResearchState  = 当前任务的结构化工作状态
Workspace      = 当前 Task 的工作记忆文件
Document       = 可跨 Task 检索的知识库
evidence_pool  = 合流后的统一证据入口
```

Agent 通过 Harness 投影的 Context Packet 获得 `workspace_path`，按需读取对应工作记忆；知识库读取使用 `doc_id / chunk_id`。

## 1. 短结果与长结果

所有产生内容的 Tool 使用同一输出原则：

```text
短结果
  → Web / SQL 将完整规范化结果写入 ToolResult.data
  → 写入 research_rounds 对应 branch

长结果
  → WorkspaceService.finalize_file
  → 返回 summary + workspace_path + content_hash + mime_type + size
  → 写入 research_rounds 对应 branch

MergeEvidence
  → 将短结果或 Workspace / Document 引用整理为 evidence_pool
```

Web / SQL 结果适合 inline budget 且不需要文件消费时直接返回；超过预算或明确作为后续文件输入时写入 Workspace。Dataset 归入 SQL/Data 能力。Compute 的计算结果、图表和派生文件始终写入 Workspace；stdout / stderr 只作为运行诊断。

## 2. 数据合同

```text
workspace_files
  tenant_id
  task_id
  workspace_path
  storage_ref
  content_hash
  mime_type
  size
  status                         # pending | ready | failed
  producer_tool_call_id?           # Compute bundle 的稳定生产调用
  created_at
  updated_at
```

Workspace 正文存储在 MinIO 的私有 `workspaces` Bucket，PostgreSQL 保存 Task scope、逻辑路径、hash、大小和发布状态。Workspace 是 Task-scoped：文件通过 `tenant_id + task_id` 关联 `ResearchTask`，用户 ownership 由该 Task 解析，不在 `workspace_files` 重复保存 `user_id`。知识库 Document 的原文件位于独立的私有 `documents` Bucket，不属于 Workspace 数据合同；实际用于研究的 Document Chunk 由 `evidence_pool` 记录。

`workspace_path` 是 Graph、Agent 和 API 使用的 Task 内逻辑路径；`storage_ref` 是 WorkspaceRepository 解析该逻辑路径后使用的对象存储物理引用。`storage_ref` 不进入 ResearchState、ToolResult、Evidence、Citation 或对外 API，也不保存临时签名 URL。

P0 容量 Settings：

```text
TENANT_WORKSPACE_BYTES_MAX=20 GiB
TASK_WORKSPACE_BYTES_MAX=1 GiB
WORKSPACE_FILE_MAX_BYTES=256 MiB
```

## 3. 发布流程

所有 Workspace producer 使用同一入口：

```text
WorkspaceService.finalize_file(
  temp_object,
  task_id,
  path,
  hash,
  size,
  mime_type,
  operation_key
)
  → 校验单文件与 Task / Tenant 配额
  → 创建或幂等 claim workspace_files(status=pending)
  → 将临时对象提升为最终对象
  → CAS 更新 workspace_files.status=ready 并提交对象元数据
  → 返回稳定 workspace_path
```

`workspace_files.status` 本身承担发布 reservation，不新增 Workspace reservation 表。发布失败时记录 `failed`；失败补偿与 Reconciler 负责临时对象、缺失对象以及未完成的 `pending / failed` 记录。Graph producer 的 `operation_key` 由 `node_run_id + effect_name` 派生；HTTP 与 Ingestion 使用各自业务幂等键，Share 使用 `share_id`，Export 使用 `export_id` 作为文件发布操作身份。

`workspaces` Bucket 使用 `{tenant_id}/{task_id}/` 前缀；`files/**`、`visualizations/**`、`reports/**` 与 `tmp/**` 都位于该 Task scope。普通读取由 API 代理，大文件可在完成 Task ownership 校验后返回短时签名 URL。

## 4. 各来源的工作记忆

### 4.1 Web

```text
search_web → URL + 短摘要
read_webpage → 抽取正文
正文适合当前 ToolResult 预算 → 直接返回
正文超过预算 → Workspace 文件 + workspace_path
```

ToolResult 同时保存 `source_uri`、`title`、`retrieved_at` 与 `content_hash`，用于形成 Evidence 与 Citation。

### 4.2 SQL / Data

```text
POST /tasks/{task_id}/data-files
  → XLSX / CSV / Parquet 原文件发布到 files/data/**

小结果集 → columns / rows / summary
长表格   → Workspace CSV/Parquet 文件 + workspace_path
```

外部数据库与上传表格都由 SQL/Data 模块查询。上传接口只返回 Workspace 引用；SQL/Data 按需将 CSV/XLSX 规范化为 Parquet，将 XLSX Sheet 映射为逻辑表，并通过 DuckDB 临时只读 View 查询。Dataset Catalog、Parquet 与 DuckDB 不形成独立 Evidence 来源。查询文本、数据源逻辑标识、执行时间、schema 与结果 hash 随结果元数据保存。

### 4.3 Compute / Visualization

Research Worker 为 Sandbox Runner 准备只读输入，Sandbox Runner 在无 PG、MinIO、Redis、ES 或 Secret 凭据的隔离环境中执行。Worker 校验受控输出目录后，通过 WorkspaceService 将计算结果、数据文件和派生文件发布到 Workspace；stdout / stderr 仅用于运行诊断，不进入 Evidence。图表与表格渲染器返回相应 `workspace_path` 和 `content_hash`。

每次 Compute 调用发布一个不可拆分的 lineage bundle：

```text
compute/{tool_call_id}/
├─ code.py                    # 仅 sandbox_execute
├─ manifest.json
└─ outputs/**
```

```yaml
manifest:
  tool_call_id: string
  analysis_id: string
  input_evidence:             # 形成正式 Compute Evidence 时非空
    - evidence_id: string
      content_hash: string
  input_datasets:             # 无 Dataset 输入时为空列表
    - dataset_ref: string
      content_hash: string
  code_hash: string?          # 仅执行代码时存在
  runtime: string
  outputs:
    - workspace_path: string
      content_hash: string
```

`input_datasets` 记录由 Dataset Catalog 解析出的执行资源血缘，不替代 Evidence。`tool_call_id` 是稳定的逻辑调用身份，retry / replay 复用同一 bundle。`workspace_files.producer_tool_call_id` 将输出定位到 manifest；MergeEvidence 为输出生成 `source_type=compute`、`derived_from=input_evidence[]`、`execution_ref=manifest.json` 的 Derived Evidence，`dataset_ref` 不写入 `derived_from`。报告仍通过 Citation 引用 Evidence，不把 manifest 建成第二事实源。

### 4.4 Report / Share / Export

报告版本、导出文件和冻结分享快照同样登记为 `workspace_files`。报告版本引用写入 `ResearchState.report`；Share 与 Export 的 `workspace_path` 分别写入 `shares` 和 `export_jobs`。

## 5. 按引用读取工作记忆

```text
ContextBuilder
  → 从 ResearchState 当前节点所需的业务字段中收集引用
  ├─ workspace_path → WorkspaceService 范围读取
  └─ doc_id / chunk_id → DocumentService 读取知识库内容
  → 按当前 Agent 的输入预算读取相关片段
  → 与 ResearchState 的结构化字段共同组成 Context Packet
```

`workspace_path`、`doc_id` 和 `chunk_id` 存在于 `research_rounds`、`evidence_pool`、`analysis_requests`、`report` 等业务对象内部，不是 `ResearchState` 顶层字段。

工作记忆的读取范围由当前节点、引用列表和 token budget 决定。完整文件保存在 Workspace，模型调用接收当前职责所需片段。

## 6. 生命周期

```text
Task 创建       → 建立 Workspace scope
Task pause      → 保留工作记忆
Task resume     → 继续使用原 Workspace 引用
Task terminal   → 保留任务产物
Task 删除       → workspace_files 进入清理流程
```

共享 Document 与用户级 Research LTM 使用各自生命周期。Workspace 临时对象和未完成发布记录由 Reconciler / GC 清理。

## 7. 可观测与错误

```text
Metrics：workspace_read_latency、workspace_write_latency、bytes_read、bytes_written、quota_exceeded
Trace：task_id、workspace_path、content_hash、mime_type、size、result
Errors：not_found、conflict、policy_denied、content_too_large、quota_exceeded、tool_error
```

## 8. 验收标准

```text
□ Workspace 表达当前 Task 的工作记忆
□ workspace_files 不重复保存 user_id；ownership 通过 tenant_id + task_id 关联 ResearchTask 解析
□ Document 表达可跨 Task 检索的知识库
□ Web / SQL 的完整短结果进入 ToolResult.data，长结果外置并返回 workspace_path
□ SQL/Data、Compute、Report、Share、Export 的文件共用 finalize_file
□ MinIO 只使用私有 documents / workspaces Bucket；Workspace 文件位于 tenant/task 前缀
□ Sandbox Runner 不直接访问存储或 Secret，输出由 Worker 校验后发布
□ Compute 输出通过 tool_call_id 找到 manifest，并回溯输入 Evidence、代码与输出 hash
□ ResearchState、ToolResult 与 evidence_pool 保存完整短结果、摘要和稳定引用
□ 同一逻辑副作用重放得到同一 ready 文件
□ workspace_files.status 直接承担 pending / ready / failed 发布状态，不新增 reservation 表
□ WorkspaceService 按引用与 token budget 读取工作记忆
□ Task pause / resume 后 workspace_path 保持有效
□ 配额、补偿、Reconciler 与 GC 覆盖文件生命周期
```
