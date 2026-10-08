# DeepResearch-mutilagent：Tool 合同

## 0. 定位

Tool 提供原子执行能力。Harness 从 ResearchState 投影当前 Agent 的 Context Packet；Agent 只根据该 Packet 与冻结的能力快照调用职责内 Tool，Graph Node 将结构化结果写回对应 branch。

```text
Per-Agent Context Packet + capability_snapshot
  → Agent 调用 Tool
  → 短结果直接返回
  → 长结果外置到 Workspace 工作记忆
  → ToolResult：summary + inline data 或 workspace_path / doc_id
  → research_rounds[depth].branch
  → MergeEvidence
  → evidence_pool
```

该链路适用于产生研究证据的 Web、Document、SQL 与 Compute Tool。Task KG 只对已有 Evidence 建立和查询派生关系，数据库写 Tool 只处理 WritePlan；两者都不向 `evidence_pool` 追加新的事实来源。

Branch Result 进入 `evidence_pool` 的准入、归一化、identity、去重与提交规则统一见 [`evidence_pipeline.md`](../01_architecture/evidence_pipeline.md)。

Document 是知识库；Workspace 是当前 Task 的工作记忆；`evidence_pool` 是统一证据入口。

## 1. Tool 清单

### Web

```text
search_web
read_webpage
render_page（动态网页 fallback）
```

### Document / RAG

```text
search_documents
search_chunks
read_document
```

### SQL / Data

```text
inspect_schema
sample_table
execute_readonly_sql
inspect_dataset
profile_dataset
query_dataset
```

### Compute

```text
sandbox_execute
generate_chart
render_table
```

### Task Knowledge Graph

```text
build_knowledge_graph
query_knowledge_graph
```

## 2. Agent Allowlist

Tool Allowlist 只包含可以经 ToolRuntime 调用的 Tool：

```text
ChiefArchitect
  无 Tool

DeepScout
  search_web, read_webpage, render_page?
  search_documents, search_chunks, read_document
  query_knowledge_graph

DataAnalyst
  inspect_schema, sample_table, execute_readonly_sql
  inspect_dataset, profile_dataset, query_dataset

CodeWizard
  sandbox_execute, generate_chart, render_table

LeadWriter
  无 Tool

CriticMaster
  无 Tool

Graph 派生更新
  build_knowledge_graph（MergeEvidence 后非阻塞调度）
```

`build_knowledge_graph` 仅供 Graph / Runtime 内部派生更新调用，不进入任何 Agent 的 `capability_snapshot.tools`。`query_knowledge_graph` 仅进入 DeepScout 的 Agent Tool allowlist。

Agent structured output 不属于 Tool：

```text
ChiefArchitect → Direct Answer / Plan / Deepen / Replan
DataAnalyst    → adopted SQL branch results / DatasetRef
CriticMaster  → CriticResult
LeadWriter    → ReportAst
```

这些输出由对应 Graph Node 校验并写入业务字段，不注册到 ToolRegistry、ToolPolicy 或 `capability_snapshot.tools`。

Workspace 文件由 Web、SQL、Compute、Report、Share 与 Export 的 producer 通过 `WorkspaceService.finalize_file` 生成。Dataset 属于 SQL/Data 能力；Sandbox、Chart 与 Table 属于 Compute。Harness 把当前职责需要的 ResearchState 引用投影到 Context Packet，Agent 据此读取工作记忆片段。

## 3. 统一运行时输入

Runtime 注入：

```text
tenant_id
user_id
task_id
node_run_id
operation_key
cancellation_token
deadline
inline_budget
```

Agent 提供 Tool 业务参数。`operation_key` 由 Runtime 根据当前 `node_run_id` 与稳定 `effect_name` 生成并注入 ToolExecutionContext，不是 Agent 参数。`inline_budget` 由 ToolRuntime 统一注入 ToolExecutionContext，producer / Adapter 据此选择内联 `data` 或 Workspace。ResourceResolver 根据 `doc_id`、`chunk_id`、`dataset_ref` 或 `workspace_path` 获取当前调用所需资源。外部 Provider 或 DataSource 的 Adapter 在本次调用通过资源授权与策略校验后，才按需解析 `secret_ref`；明文凭据不进入 ToolExecutionContext。

## 4. 统一输出

```text
ToolResult:
  status
  summary
  data?                    # 预算内结构化结果
  references[]:
    source_uri?
    retrieved_at?
    doc_id?
    chunk_id?
    workspace_path?
    content_hash?
    mime_type?
    size?
  truncated?
  next_cursor?
  error?
```

输出策略：

```text
Web 短文本 / SQL 小表格或小型结构化结果
  → 完整规范化结果写 data

网页长正文 / SQL 长表格 / Compute 文件 / 图表 / 报告
  → WorkspaceService.finalize_file
  → references[].workspace_path

Sandbox / Compute
  → stdout / stderr 仅作运行诊断
  → 计算结果必须作为 Workspace 文件返回

知识库检索结果
  → references[].doc_id / chunk_id
```

ToolResult、`research_rounds` 和 `evidence_pool` 保存摘要、完整短结果与稳定引用。`truncated / has_more / next_cursor` 等完整性标记随短结果保留；这些字段表达上游分页或硬限制，不用于把超出 inline budget 的完整内容主动截短。

inline budget 的职责固定为：

```text
ToolRuntime
  → 向 ToolExecutionContext 注入统一 inline_budget
  → normalize 后校验 ToolResult 是否符合预算与 data/reference 互斥规则

producer / Adapter
  → 按 inline_budget 在生成结果时选择完整 inline data 或 Workspace
  → 可预见超限时直接流式写入 Workspace，不先物化完整大结果再交给 Runtime 搬运
```

超过预算，或明确需要文件作为后续 Tool 输入时，producer 返回 Workspace 引用；同一完整结果不同时保留 inline 与 Workspace 两份副本。

## 5. Tool 调用链

```text
Agent
  → ToolRuntime
  → Registry lookup
  → Agent allowlist
  → 参数 schema
  → Resource scope / policy
  → timeout / cancellation
  → LocalToolAdapter 或 MCPToolAdapter
  → ToolResult normalize
  → Trace / metrics
```

Local Tool 在服务进程内执行；外部 MCP Server 通过 MCP Adapter 接入并归一为同一 ToolResult。

Local、MCP 以及后续 Adapter 实现同一 `ToolAdapter.execute(ToolExecutionContext, normalized_arguments) → ToolResult` 合同。ToolRuntime 不包含 Provider 专用分支；`provider_ref` 解析出快照锁定的 Adapter 与配置版本。

## 6. Web / Document / 数据结果

### Web

```text
search_web → URL + 短摘要
read_webpage → 短正文或 Workspace 长正文引用
```

### Document / RAG

```text
search_documents → 文档级发现
search_chunks    → Hybrid RAG 命中
read_document    → 已知 Document 的定点读取
```

### SQL / Data

```text
外部数据库
  → inspect_schema / sample_table / execute_readonly_sql

XLSX / CSV / Parquet
  → SQL/Data 模块解析并签发 dataset_ref
  → inspect_dataset / profile_dataset / query_dataset

查询结果适合 ToolResult 预算 → columns / rows / summary
查询结果超过预算          → Workspace 表格文件 + workspace_path
```

Dataset 只在 SQL/Data Tool 内区分资源与执行方式；进入 `research_rounds.sql` 与 `evidence_pool` 后统一使用 SQL 来源。

### Sandbox / Chart / Table

Sandbox 只接受 Document / Workspace 文件输入。其 stdout / stderr 不作为 Evidence；计算结果、图表和表格文件经 `WorkspaceService.finalize_file` 发布并返回 `workspace_path`。若 Web / SQL 结果明确需要交给 Sandbox 继续处理，producer 直接选择 Workspace 输出，不同时保留一份 inline Evidence。

### Task Knowledge Graph

`build_knowledge_graph` 从当前 Task 的 Evidence 构建派生关系索引；`query_knowledge_graph` 以 ToolResult.data 返回 Claim 及其 `evidence_id`。KG 结果供当前分析调用使用，不进入 MergeEvidence，不形成新的 Evidence 来源；所有事实仍通过原 Evidence 回溯 Document、内联结果或 Workspace。

## 7. 副作用幂等

Graph 节点内的副作用键：

```text
operation_key = sha256(
  tenant_id | task_id | node_run_id | effect_name
)
```

`effect_name` 使用工具名与稳定目标槽，例如：

```text
read_webpage:{canonical_url}
sql_result:{query_id}
compute_output:{analysis_id}:{logical_call_slot}
report_version:{revision_round}
```

`effect_name = effect type + stable semantic target`。同一 AnalysisRequest 只允许一次 Compute 调用时可以省略 `logical_call_slot`；允许多次调用时必须使用可由 Plan/AnalysisRequest 重放得到的稳定槽位。`task_id` 已包含在 operation key 外层，不能再作为 Compute 的唯一目标槽；随机 `tool_call_id`、attempt ID 或其他重放时无法重建的值也不能充当目标槽。

HTTP、Ingestion、Share 与 Export 使用各自 Source of Truth 定义的业务幂等键。

## 8. Retry 与错误

```text
只读调用      → 依据错误分类、预算和 Circuit Breaker 执行 retry / fallback
Workspace 发布 → 依据 idempotency key 恢复或复用发布结果
业务写        → WritePlan / HITL / WriteExecutor 的领域幂等
```

统一错误分类：

```text
invalid_argument
policy_denied
not_found
conflict
timeout
rate_limited
provider_auth_error
provider_unavailable
limit_exceeded
tool_error
cancelled
```

DNS、连接、TLS、代理、连接重置和 Provider 5xx 等外部不可用原因统一归一为 `provider_unavailable`。底层 `cause` 只写失败事件或日志，用于区分 `dns | connect | tls | proxy | reset | http_5xx | provider_down`，不扩充 Agent 可见的 ToolError。

## 9. 可观测

```text
tool.name
research.agent?
research.node_run_id
error.type?                    # 最终失败
```

`tool.execute` 表达一次逻辑 Tool 调用；Provider retry / fallback 写入 `tool.retry`、`tool.fallback` Event，实际 HTTP、数据库或 MCP 请求使用 CLIENT Span。Span 原生 duration 与 status 是耗时和结果状态的唯一观测字段。`workspace_path / doc_id` 保存在 ToolResult 与 Evidence，不作为常规 Trace 属性。Circuit Breaker 只在拒绝调用或状态切换时写 `circuit_breaker.rejected / circuit_breaker.state_changed` Event 及对应计数指标。完整字段见 [`observability.md`](../05_quality/observability.md)。

## 10. 验收标准

```text
□ Tool Allowlist 只包含可调用 Tool；PrepareWrite、ReportAst、CriticResult 等 structured output 不进入 ToolRegistry
□ Web、Document、SQL 与 Compute Tool 使用统一 ToolResult
□ Web / SQL 的完整短结果直接返回，长结果与 Compute 产物进入 Workspace 工作记忆
□ Document 引用表达知识库内容
□ Workspace 引用表达当前 Task 工作记忆
□ research_rounds 经 MergeEvidence 形成 evidence_pool
□ ChiefArchitect 无 Tool；所有业务 Tool 只由专业执行 Agent 或 Graph 派生节点调用
□ Task KG 只保存 Evidence 的派生关系，查询结果通过 evidence_id 回源
□ 所有 Workspace producer 使用 finalize_file
□ producer 按 ToolRuntime 注入的 inline_budget 选择 inline 或 Workspace，ToolRuntime 负责 normalize 与最终校验
□ Node 副作用重放复用稳定 operation_key；effect_name 能区分同一 NodeRun 内的多个稳定语义调用槽
□ ToolRuntime 统一完成 schema、scope、policy、timeout 与观测
□ Provider 传输故障统一为 provider_unavailable，具体 cause 仅进入失败事件/日志
```
