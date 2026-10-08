# DeepResearch Multi-Agent 文档与实现路线图

本文是本仓库的**文档入口、Source of Truth 地图和施工顺序说明书**。实现者按 Phase 顺序推进，每阶段通过验收后进入下一阶段。

## 1. 项目架构概览


核心 Research Graph：

```text
用户输入 + 同 Thread 近期消息 → ResearchState.query
  ↓
RouteNode
  ├─ direct  → ChiefArchitect 无工具回答 → assistant Message
  ├─ focused → DeepScout | DataAnalyst | CodeWizard → assistant Message
  └─ deep
       → ChiefArchitect Plan（Hypotheses + Web/RAG/SQL Queries）
       → Web // RAG // SQL
       → MergeEvidence
       → Post-Merge Analysis Decision
            └─ 按需 AnalysisRequest → CodeWizard → Compute branch → MergeEvidence → Derived Evidence
       → CoverageGate（结构完整性）
            ├─ 不完整且有预算 → ChiefArchitect Deepen → 下一轮 Retrieval
            └─ 达到 stop condition → Failed
       → CriticMaster（语义充分性）
            ├─ retrieve + budget → 指定 Web/RAG/SQL 补证
            ├─ deepen + budget   → ChiefArchitect 扩充 Plan
            ├─ gap + stop condition → Failed
            └─ pass
                 → DatabaseWriteGate / PrepareWrite（存在 write_intent 时）
                 → LeadWriter
                 → Report Schema Validation + CitationValidation
                      ├─ 无效且有预算 → Writer Revise
                      └─ 有效 → Finalize
```

固定六个专家 Agent：

| Agent | 职责 |
|---|---|
| ChiefArchitect | direct 无工具回答；deep 路径的 Plan / Deepen / Replan |
| DeepScout | Web 搜索、网页读取、Document RAG 与来源引用 |
| DataAnalyst | 业务库、Task data-file 与 SQL/DuckDB 查询，输出 adopted SQL branch results / DatasetRef |
| CodeWizard | 消费 AnalysisRequest，在 Sandbox 中完成计算、文件加工和图表生成 |
| CriticMaster | Writer 前审核证据对 Hypothesis 和原问题的语义充分性 |
| LeadWriter | 基于已通过审核的 `evidence_pool` 生成/修订 ReportAst |


核心基础设施固定为：

```text
PostgreSQL        权威结构化状态、业务元数据、checkpoint/outbox
Redis             Celery broker、SSE live fan-out 与可丢失 SemanticCache
MinIO             Document 原文、Workspace 内容、导出文件
Elasticsearch+IK  同一引擎完成 BM25/IK + dense_vector 召回；应用层 RRF 为 P0 默认
Neo4j             Task Research KG 派生索引
```


## 2. Source of Truth

当文档出现交叉主题时，以下文件拥有最终解释权；其他文档只描述调用关系。

| 主题 | Source of Truth |
|---|---|
| Research Graph 节点与路由 | [graph.md](01_architecture/graph.md) |
| ResearchState Schema、状态流转、depth/revision、checkpoint、写权限 | [state.md](01_architecture/state.md) |
| Branch Result 准入、来源归一化、Evidence identity、去重与 MergeEvidence 提交 | [evidence_pipeline.md](01_architecture/evidence_pipeline.md) |
| Route、Hypothesis Plan、Coverage、Deepen、Critic Gate | [planning.md](01_architecture/planning.md) |
| 六 Agent 职责、输入输出、能力范围、协作 | [agents.md](01_architecture/agents.md) |
| Skill Runtime 与 MCP 外部能力接入 | [capability.md](02_capabilities/capability.md) |
| Tool Registry、ToolResult、allowlist、公共执行链 | [tool.md](02_capabilities/tool.md) |
| `search_web/read_webpage`、短结果返回与长结果 Workspace 外置 | [web_tools.md](02_capabilities/tools/web/web_tools.md) |
| Document 进入、解析、Chunk、Index | [document_system.md](02_capabilities/document_system.md) |
| 已索引内容如何检索 | [rag.md](02_capabilities/rag.md) |
| Task-scoped Research Workspace | [research_workspace.md](02_capabilities/research_workspace.md) |
| ReportAst、Claim/Citation、引用验证、版本与统一渲染 | [report_citation.md](02_capabilities/report_citation.md) |
| Evidence 文件承载边界 / Research LTM | [memory.md](02_capabilities/memory.md) |
| 外部 PostgreSQL DataSource、Grant、Policy、Credential 与连接池 | [datasource.md](02_capabilities/datasource.md) |
| WritePlan→HITL→WriteExecutor | [database_write.md](02_capabilities/database_write.md) |
| timeout/retry/CB/lease/pause/cancel/recovery | [runtime.md](03_runtime/runtime.md) |
| HTTP 资源与 SSE 合同 | [api_lifecycle.md](04_platform/api_lifecycle.md) |
| Tenant/User/Ownership/资源 scope | [auth_tenant.md](04_platform/auth_tenant.md) |
| JWT、Secret、SSRF、安全分层 | [auth_security.md](04_platform/auth_security.md) |
| ModelConfig→Role Binding→Resolver→ModelRuntime→Provider Client | [llm.md](04_platform/llm.md) |
| PG/Redis/MinIO/ES/Neo4j 数据归属 | [storage.md](04_platform/storage.md) |
| Compose/Nginx/Worker/队列/部署恢复 | [deployment.md](04_platform/deployment.md) |
| Trace/Cost/Audit | [observability.md](05_quality/observability.md) |
| 工程测试分层、Fixture、Testcontainers 与 CI Gate | [testing.md](05_quality/testing.md) |
| RAG/Research LTM/E2E/Skill 四类评测 | [evaluation.md](05_quality/evaluation.md) |
| Tool Decision Trace→治理/标注→Dataset→QLoRA/SFT→Gate→Shadow/Canary 闭环 | [data_flywheel.md](05_quality/data_flywheel.md) |
| 前端交互 | [frontend.md](06_product/frontend.md) |
| 冻结分享与导出 | [share_export.md](06_product/share_export.md) |

## 3. 文档地图

### 3.1 Architecture

- [graph.md](01_architecture/graph.md)：定义 Research Graph 的专家节点、控制节点和主路由。
- [state.md](01_architecture/state.md)：定义唯一 ResearchState、写权限、原子提交、checkpoint 与恢复语义。
- [evidence_pipeline.md](01_architecture/evidence_pipeline.md)：定义 adopted branch results 如何经过准入、归一化、稳定身份与原子提交成为正式 Evidence。
- [planning.md](01_architecture/planning.md)：定义研究计划、证据需求、覆盖检查和 Deepen 查询合同。
- [agents.md](01_architecture/agents.md)：定义固定六专家的职责、读写、Tool/Skill 范围与 Prompt Contract。

### 3.2 Capabilities

- [capability.md](02_capabilities/capability.md)：定义 Skill Runtime、渐进披露、MCP Adapter 与专家能力绑定。
- [tool.md](02_capabilities/tool.md)：定义 Web、Document、Data、Compute 与 Task KG Tool、统一执行链和 allowlist。
- [document_system.md](02_capabilities/document_system.md)：定义 Document 的接入、解析、Chunk、索引、删除与恢复。
- [rag.md](02_capabilities/rag.md)：定义 ES Hybrid Retrieval、Scope、Fusion、Rerank 和 Parent/Child 展开。
- [research_workspace.md](02_capabilities/research_workspace.md)：定义任务级工作记忆文件系统、长结果外置、存储合同和生命周期。
- [memory.md](02_capabilities/memory.md)：定义 evidence_pool、检索长结果文件外置、Document/Workspace 范围与单表 Research LTM。
- [datasource.md](02_capabilities/datasource.md)：定义外部 PostgreSQL DataSource 元数据、用户授权、表列策略、凭据与连接池。
- [database_write.md](02_capabilities/database_write.md)：定义受控数据库写入的异步 HITL 闭环。
- [tools/README.md](02_capabilities/tools/README.md)：Tool 子文档入口与统一模板。

Web Tools：

- [web_tools.md](02_capabilities/tools/web/web_tools.md)：定义 `search_web → read_webpage → 短结果直接返回 / 长结果写 Workspace` 主链及 Provider、安全与测试合同。
- [render_page.md](02_capabilities/tools/web/render_page.md)：定义 P1 动态网页读取 fallback 的浏览器隔离、SSRF、资源上限与归档合同。

Document Tools：

- [search_documents.md](02_capabilities/tools/document/search_documents.md)：在授权范围做文档级 metadata 发现。
- [search_chunks.md](02_capabilities/tools/document/search_chunks.md)：Agent 调用完整 Hybrid RAG 的薄接口。
- [read_document.md](02_capabilities/tools/document/read_document.md)：按 `doc_id + chunk_id` 定点读取知识库切片。

Data Tools：

- [inspect_schema.md](02_capabilities/tools/data/inspect_schema.md)：有界发现业务库 schema/table/column。
- [sample_table.md](02_capabilities/tools/data/sample_table.md)：安全读取少量表样本行。
- [execute_readonly_sql.md](02_capabilities/tools/data/execute_readonly_sql.md)：执行 AST 校验、allowlist 和 DB 只读双保险查询。
- [inspect_dataset.md](02_capabilities/tools/data/inspect_dataset.md)：探查受控 Dataset 的结构、类型与缺失概览。
- [profile_dataset.md](02_capabilities/tools/data/profile_dataset.md)：按固定口径生成分布与 IQR 异常画像。
- [query_dataset.md](02_capabilities/tools/data/query_dataset.md)：只对绑定逻辑表 `dataset` 执行受限 DuckDB SELECT。

Compute Tools：

- [sandbox_execute.md](02_capabilities/tools/compute/sandbox_execute.md)：CodeWizard Compute 分支的隔离 Python 执行。
- [generate_chart.md](02_capabilities/tools/compute/generate_chart.md)：将受控结构化数据渲染为 Workspace 图表。
- [render_table.md](02_capabilities/tools/compute/render_table.md)：将受控数据渲染为可追溯表格产物。

Task KG Tools：

- [build_knowledge_graph.md](02_capabilities/tools/knowledge_graph/build_knowledge_graph.md)：基于 Entity+Claim+Provenance 增量构建任务级 KG。
- [query_knowledge_graph.md](02_capabilities/tools/knowledge_graph/query_knowledge_graph.md)：有界查询当前任务 KG 并返回 Claim 与原文 provenance。

### 3.3 Runtime

- [runtime.md](03_runtime/runtime.md)：定义 TaskRuntime、级联预算、lease、pause/cancel、retry、NodeRun 原子提交与恢复。

### 3.4 Platform

- [api_lifecycle.md](04_platform/api_lifecycle.md)：定义资源 API、任务命令、SSE 事件和 Service 职责。
- [auth_tenant.md](04_platform/auth_tenant.md)：定义授权根、子资源继承、允许候选和各后端的执行方式。
- [auth_security.md](04_platform/auth_security.md)：定义 JWT、对象级授权入口、Secret 与外部网络安全。
- [llm.md](04_platform/llm.md)：定义模型配置、解析、调用、Provider Adapter 和 fallback。
- [storage.md](04_platform/storage.md)：定义各存储的权威/派生数据与跨存储写入顺序。
- [deployment.md](04_platform/deployment.md)：定义 Compose 拓扑、网络、队列、持久卷、启动与恢复。

### 3.5 Quality

- [observability.md](05_quality/observability.md)：定义 OTel Trace、成本归因、Audit 与隐私。
- [testing.md](05_quality/testing.md)：定义测试目录、四层工程测试、同栈 Testcontainers、Fake Provider 和 CI Gate。
- [evaluation.md](05_quality/evaluation.md)：定义 RAG、Research LTM、E2E 三类 EvalRun、规则指标与 LLM Judge。
- [data_flywheel.md](05_quality/data_flywheel.md)：定义三个执行 Agent 的 Tool DecisionTrace、Eligibility / Governance、Label / Sample Builder、不可变 Dataset、Offline Tool Eval / Safety Gate，以及 QLoRA/SFT、Shadow、Canary 与新 Tool Model Binding 的闭环。

### 3.6 Product

- [frontend.md](06_product/frontend.md)：定义三栏研究 UI、HITL、状态恢复和后端映射。
- [share_export.md](06_product/share_export.md)：定义冻结 Snapshot 分享、token 与写入 Workspace 的报告导出文件。

## 4. 推荐阅读顺序

首次接手项目按下面顺序阅读，先理解业务主链，再进入能力、运行时和产品层：

```text
README
  ↓
graph.md → state.md → planning.md → agents.md
  ↓
auth_tenant.md → storage.md → datasource.md → llm.md
  ↓
capability.md → tool.md → 各 Tool Contract
  ↓
document_system.md → rag.md → research_workspace.md → report_citation.md → memory.md
  ↓
runtime.md → api_lifecycle.md
  ↓
testing.md → observability.md → evaluation.md → data_flywheel.md
  ↓
frontend.md → share_export.md → deployment.md
```


## 5. 实施规则

3. 先实现正常主流程，再补该 Phase 文档已列出的异常、恢复和安全范围。
5. OTel 埋点接口从底层 Phase 即保留；完整采集、成本与 Audit 在质量 Phase 验收。
6. Prompt 正文随源码放在 `app/prompts/*.jinja2`；本文档只维护 Prompt Contract。

## 6. 实现顺序

### Phase 0：工程骨架与本地基础设施

**目标**

建立可启动、可迁移、可测试的最小工程和与生产同类型的本地基础设施。

**前置依赖**

无。

**本阶段阅读**

- [deployment.md](04_platform/deployment.md)
- [storage.md](04_platform/storage.md)
- [auth_security.md](04_platform/auth_security.md) 的 Secret/配置范围
- [testing.md](05_quality/testing.md)

**实现**

- 应用包骨架：API、Worker、Domain/Application/Adapter 范围和配置加载。
- Docker Compose：PostgreSQL(+pgvector)、Redis、MinIO、Elasticsearch+IK、Neo4j；Neo4j 只承载 Task KG，不承载研究记忆。
- Alembic 迁移骨架、`tests/unit|contract|integration|e2e`、统一 lint/type/test 命令和 CI 基线。
- Testcontainers 使用共享的生产同栈 PG(+pgvector)/Redis/MinIO/ES+IK/Neo4j；每个 Case 通过唯一业务 ID 验证真实 scope 隔离。
- 外部 LLM/Embedding/Reranker/Web/MCP 使用协议级确定性 Fake，调用仍经过真实 ModelRuntime、ToolRuntime 与 Adapter。
- MinIO bucket、ES index template/IK、PostgreSQL(+pgvector) 与 Neo4j 健康检查。

**验收标准**

- 一条命令启动全部基础设施，health check 全绿。

**下一阶段依赖它什么**

Phase 1 需要稳定配置、迁移、连接与测试基线。

### Phase 1：身份、安全与存储基础合同

**目标**

先建立所有后续模块都依赖的授权根、父子资源继承和存储 Repository 范围。

**前置依赖**

Phase 0 基础设施与迁移可用。

**本阶段阅读**

- [auth_tenant.md](04_platform/auth_tenant.md)
- [auth_security.md](04_platform/auth_security.md)
- [storage.md](04_platform/storage.md)
- [datasource.md](02_capabilities/datasource.md) 的元数据、Grant、Policy 与 Secret 范围

**实现**

- 最小 `AuthContext`、按入口构造的 ExecutionContext、JWT 验证、授权根 Guard 与统一 not-found 防枚举语义。
- Tenant/User/Thread/Task、DataSource/Grant/Policy 等基础表及 Repository；所有业务表强制 `tenant_id`。
- SecretsService（成熟加密库）、外部网络 SSRF Policy 和凭据注入范围。

**验收标准**

- 同租户同用户、同租户异用户、跨租户的资源访问矩阵全部有自动化测试。
- Repository 查询默认携带 tenant scope，遗漏时测试失败。

**下一阶段依赖它什么**

State、Document、Workspace、Tool 与 API 都复用同一授权根规则，并按各入口构造所需上下文。

### Phase 2：ResearchState、任务持久化与原子提交

**目标**


**前置依赖**

Phase 1 的身份、PG Repository 和 Unit of Work。

**本阶段阅读**

- [state.md](01_architecture/state.md)
- [graph.md](01_architecture/graph.md) 的 Node Contract
- [planning.md](01_architecture/planning.md) 的 Plan/Coverage 数据结构

**实现**

- `ResearchState`、Plan、Coverage、Evidence、Report、Critic、Reliability schema 与版本化序列化。
- `StateManager`：合法状态迁移、字段级写权限、revision/CAS、branch patch merge。
- `research_tasks` 创建幂等字段、soft-delete tombstone、`lease_token` 与唯一 `execution_budget_remaining_ms`。
- LangGraph checkpointer Adapter；NodeRun completion、State patch、checkpoint 与 outbox 在验证当前 `lease_token` 后原子提交。
- `pausing/cancelling` 协作控制状态、`research_events/outbox_messages`、幂等 command/event key。

**验收标准**

- Schema round-trip、非法状态迁移、越权字段写、重复事件均有测试。
- Web/RAG/SQL 分支并发 patch 可确定性 merge，Compute 在首次证据合流后执行；各分支写入当前 `research_rounds[depth]`。

**下一阶段依赖它什么**

Agent/Graph、Runtime、SSE 和 HITL 都需要稳定 State/commit/outbox Contract。

### Phase 3：LLM、Tool、Skill 与 MCP 基础层

**目标**


**前置依赖**

Phase 1 的 ExecutionContext/Secrets，Phase 2 的 Trace/State 引用合同。

**本阶段阅读**

- [llm.md](04_platform/llm.md)
- [capability.md](02_capabilities/capability.md)
- [tool.md](02_capabilities/tool.md)
- [auth_security.md](04_platform/auth_security.md)
- [runtime.md](03_runtime/runtime.md)（Tool retry / CB 与 TaskBudget）

**实现**

- ModelConfig Repository、角色 Model Binding、ModelResolver、ModelRuntime、Provider Client 与冻结 fallback。
- ToolRegistry、统一 ToolResult/错误分类、ToolRuntime 与 Policy；按 `runtime.md` 的执行合同实现 Tool timeout/retry/CB 机制。
- Skill Catalog/Loader/Activation：渐进披露、同轮最多 2 个 Skill、required_tools 校验。
- 基于 MCP 官方 SDK 的 Adapter：`tools/list`→Registry、按 Task 快照 available_tools、`tools/call`、SSRF/credential/policy。

**验收标准**

- 角色 binding 可复用 ModelConfig，Task 创建后冻结 primary/fallback revision。
- Local/MCP Tool 通过同一 ToolRuntime、Policy 和 ToolResult 合同执行。
- Task 恢复复用创建时冻结的 Skill/Tool/Provider 能力快照。

**下一阶段依赖它什么**

Document/RAG 需要 embedding/rerank；所有具体 Tool 与 Agent 需要 Registry/ToolRuntime/Skill/MCP。

### Phase 4：Document Ingestion 与 RAG 闭环

**目标**

完成 `Document → Parse → Chunk → Embed/Index → Hybrid Retrieval → 定点回读` 的可验证闭环。

**前置依赖**

Phase 0 的 MinIO/ES，Phase 1 的 Ownership，Phase 3 的模型与 Tool 底座。

**固定实现合同**

- BM25/IK 与 kNN 候选使用 RRF 合并，再经 Rerank 与 Parent Expansion；原生与应用层 RRF 共享同一 RetrievalResult 合同。
- `search_chunks.top_k` 默认 `10`、上限 `20`；`read_document.max_chars` 默认 `12,000`、上限 `40,000`。
- `text_v1`：Parent `1200`、Child `400`、Overlap `80` tokens；`BAAI/bge-m3` / `1024` 维，首版 rerank 默认关闭。

**本阶段阅读**

- [document_system.md](02_capabilities/document_system.md)
- [rag.md](02_capabilities/rag.md)
- [search_documents.md](02_capabilities/tools/document/search_documents.md)
- [search_chunks.md](02_capabilities/tools/document/search_chunks.md)
- [read_document.md](02_capabilities/tools/document/read_document.md)
- [evaluation.md](05_quality/evaluation.md) 的 RAG 指标定义

**实现**

- Document/Chunk schema、tenant scope 与 DocumentService；“知识库”是 tenant 级 Document 产品入口。
- 上传预登记、MinIO 写入、每个 Document 一条当前 Ingestion 控制记录、Scanner/Parser、结构感知 Parent/Child Chunker。
- `text_v1` 固定 Parser/Normalize/结构/段落/句子/tokenizer manifest；超长 Parent 依次按段落、句子、tokenizer hard window 切分。
- ModelRuntime 生成向量；ES `document_chunks` 保存 Chunk text + dense_vector，并通过 generation 与 published_generation 支持失败恢复和索引重建。
- RetrievalGateway：tenant_id + published_generation → BM25/IK + kNN → 应用层 RRF → Rerank → Parent Expansion。
- 三个 Document Tool 及稳定 citation/content_hash。

**验收标准**

- 上传一个 PDF 后 Ingestion 从 queued→processing→succeeded，Document 从 pending→ready，PG/MinIO/ES 可追溯一致。
- 最小 RAG golden set 达到本项目设定的 Phase gate。

**下一阶段依赖它什么**

DeepScout、Workspace 工作记忆、Writer citation 和 E2E Graph 依赖稳定 Document/RAG 引用。

### Phase 5：Research Workspace 与 Memory

**目标**

实现 Task-scoped Research Workspace、检索长结果文件外置、Thread 对话回放，以及 MemoryService/PG 单表 Research LTM（`research_memories` + pgvector），并保持证据、文件和长期记忆范围清晰。

**前置依赖**

Phase 1 存储/Ownership/Secrets/SSRF，Phase 2 Task/Thread，Phase 3 ToolRuntime，Phase 4 Document 引用。

**固定实现合同**

- `conversation_messages` 保存用户原文；任务创建与 Replan 以当前用户消息为截止点读取同 Thread 有界近期消息，将补全结果直接写入唯一的 `ResearchState.query`。
- Memory Extractor 只把用户消息作为长期记忆事实依据；final assistant message 只提供轮次语境。
- LTM scope 固定 `tenant_id+user_id`，可跨 Task；durable-memory write gate、exact dedup、单表冲突状态、统一 Top-8/1200-token 召回和 Research LTM Eval 全部以 `memory.md` 为真源。
- `MergeEvidence` 只写 `evidence_pool`；知识库内容由 Document 提供，Web / SQL 的完整短结果内联保存，长结果与 Compute 文件写 Workspace。
- Workspace 配额：Tenant `20 GiB`、Task `1 GiB`、单文件 `256 MiB`。

**本阶段阅读**

- [research_workspace.md](02_capabilities/research_workspace.md)
- [memory.md](02_capabilities/memory.md)
- [web_tools.md](02_capabilities/tools/web/web_tools.md)

**实现**

- WorkspaceService、`workspace_files`、MinIO 对象、Resource Resolver 与 ContextLoader；显式 Task Document 绑定由 Document/Task 关系管理。
- Web / SQL 长结果、Compute 文件、Report、Share 与 Export 的统一 Workspace 外置与 `finalize_file`；Dataset 归入 SQL/Data 能力。
- `search_web` 返回 URL+摘要；`read_webpage` 的短正文直接返回，长正文写入 Workspace 并返回 `workspace_path`。
- 所有 SQL/Data、Compute、Report、Share 与 Export 文件统一调用 `WorkspaceService.finalize_file`；登记 `workspace_files` 后才计入并通过 quota。
- Workspace 生命周期/GC 与 Document 引用保护。

**验收标准**

- 两个 Task 的同名路径完全隔离；`../`、绝对路径、symlink 逃逸失败。
- 相同逻辑副作用重放复用同一 Workspace 文件。
- Redis 清空后研究记忆仍可从 `research_memories` + pgvector 读出。
- Search 候选保持为短结果；Read 的长正文幂等外置到 Workspace，分页读取与 `workspace_path` 可稳定续用。

**下一阶段依赖它什么**

数据/计算 Tool 需要 Workspace 落派生结果；ChiefArchitect 在 Plan/Replan/Deepen 前受控召回 Research LTM，Writer 只消费 evidence_pool 并按引用读取文件。

### Phase 6：结构化数据、Compute 与受控写库

**目标**

完成 DataAnalyst/CodeWizard 所需的可执行 Tool，并闭合唯一数据库写入通道。

**前置依赖**

Phase 3 ToolRuntime，Phase 5 Workspace，Phase 1 Security/Ownership，Phase 2 可暂停状态。

**固定实现合同**

- readonly SQL 默认/最大行为 `200/1000`，timeout `10/30 s`；profile 默认/最大列数 `50/100`。
- Sandbox 为 `2 vCPU / 2 GiB RAM / 2 GiB writable disk / 64 PIDs / 120 s default / 300 s max`，stdio `4 MiB`，单文件 `256 MiB`、总输出 `512 MiB`，`network=false`。
- Chart 建议/最大点数 `5,000/20,000`、序列 `20`、类别 `200`；Table 默认/最大行 `100/500`、列 `30`、单元格 `4 KiB`、序列化结果 `256 KiB`。

**本阶段阅读**

- [database_write.md](02_capabilities/database_write.md)
- [datasource.md](02_capabilities/datasource.md)
- [Data Tool 目录](02_capabilities/tools/data/)
- [Compute Tool 目录](02_capabilities/tools/compute/)
- [tool.md](02_capabilities/tool.md) 的权限/副作用规则

**实现**

- inspect/sample/readonly SQL 三件套，成熟 SQL Parser AST、列级 allowlist、DB read-only 双保险。
- `POST /tasks/{task_id}/data-files` 将 XLSX/CSV/Parquet 流式发布为 Task Workspace File，应用层精确限制 256 MiB。
- inspect/profile/query Dataset 三件套，DuckDB 读取绑定的逻辑表。
- Sandbox Runtime Adapter、CodeWizard Compute policy、只读输入与原子输出提交。
- deterministic chart/table renderer 与 Workspace provenance。
- WritePlan validator、Preflight/Preview、PendingWrite、Approval command、WriteExecutor、WriteResult/Audit。
- 目标业务库 `agent_write_idempotency`（或仅 INSERT 的已验证业务 UNIQUE 键）与 DML 在同一外部事务；commit-unknown 先按 operation_key 查账。

**验收标准**

- affected_rows、事务回滚、幂等、actor audit 测试通过。

**下一阶段依赖它什么**

六 Agent 必须在具体 Tool 可调用后才能做真实 prompt/tool integration。

### Phase 7：六大专家 Agent 与 Prompt Contract

**目标**


**前置依赖**

Phase 2 State schema，Phase 3 Model/Tool/Skill，Phase 4-6 具体能力。

**本阶段阅读**

- [agents.md](01_architecture/agents.md)
- [planning.md](01_architecture/planning.md)
- [tool.md](02_capabilities/tool.md) §1.1
- [capability.md](02_capabilities/capability.md)
- [report_citation.md](02_capabilities/report_citation.md)

**实现**

- RouteNode 的结构化分类 Prompt，以及六个 Agent 按职责拆分的 Prompt Contract。
- 每个 Agent 的 context builder、structured output schema 和校验器。
- ChiefArchitect Direct/Plan/Deepen/Replan、DeepScout 检索决策、DataAnalyst adopted SQL branch results/DatasetRef、CodeWizard AnalysisResult、CriticResult 与 Writer ReportAst。

**验收标准**

- RouteNode 不是 Agent；固定六个 Agent，且只有 DeepScout/DataAnalyst/CodeWizard 使用业务 Tool。
- AnalysisRequest 只由 Post-Merge Analysis Decision 产生；DataAnalyst 不产生计算请求。
- LeadWriter 只读 Workspace、主读 evidence_pool、只产 ReportAst candidate；报告管线成功发布完整版本包后才提交 draft/version。
- Agent 输出能被 schema 稳定解析；错误输出进入受控重试而非污染 State。

**下一阶段依赖它什么**

Research Graph 需要可独立测试的六个 Agent node implementation。

### Phase 8：Research Graph 主链与端到端研究闭环

**目标**

把已有 State、Tool 和 Agent 组装为可 checkpoint、可 Deepen、可执行 Writer 修订的完整 LangGraph。

**前置依赖**

Phase 2-7 全部通过。

**本阶段阅读**

- [graph.md](01_architecture/graph.md)
- [state.md](01_architecture/state.md)
- [evidence_pipeline.md](01_architecture/evidence_pipeline.md)
- [planning.md](01_architecture/planning.md)
- [agents.md](01_architecture/agents.md)
- [report_citation.md](02_capabilities/report_citation.md)
- [runtime.md](03_runtime/runtime.md) 的节点范围

**实现**

- RouteNode 的 direct/focused/deep 三条路径，以及 deep 路径的 Plan、Web、RAG、SQL 节点。
- MergeEvidence、Post-Merge Analysis Decision、AnalysisRequest/Compute、Coverage、Deepen 与 depth 门禁。
- Writer 前 Critic Gate；LeadWriter、Report Schema/Citation Validation、Writer Revise 与 Finalize。
- Fake tool/provider 下的完整 Graph integration suite。

**验收标准**

- Coverage 只检查结构完整性，Critic 只判断语义充分性；Critic 的 retrieve channel 仅为 Web/RAG/SQL。
- Writer 后不再调用 Critic；Schema/Citation 无效且仍有 revision 预算时进入 Writer Revise，通过后 Finalize。
- Fake E2E 从 query 到 final report 全链通过。

**下一阶段依赖它什么**

Runtime/Async 需要一个完整、可恢复的 Graph 执行体。

### Phase 9：Task Runtime、可靠性与粗粒度异步 Job

**目标**

让完整 Graph 在 Worker 崩溃、超时、暂停、取消和外部故障下可控恢复。

**前置依赖**

Phase 8 Graph 与 Phase 2 checkpoint/outbox。

**本阶段阅读**

- [runtime.md](03_runtime/runtime.md)
- [deployment.md](04_platform/deployment.md) 的队列、Worker 与 outbox-dispatcher
- [state.md](01_architecture/state.md) 的 control/reliability 字段

**实现**

- 固定首版运行参数：Worker slots `2`，user/tenant running quota `2/4`，user/tenant pending quota `20/100`，branch concurrency `3`。
- 建立 research/ingestion/export 三队列；消息仅携带全局唯一的 `task_id/doc_id/export_id`，Worker 从 PostgreSQL 重建 scope 并完成领域 claim。
- 由 `outbox-dispatcher` 发布 Job/Event；Recovery Loop 只根据 PG 领域状态补写调度 Outbox。
- Pause/Cancel 由当前 Research Worker 协作收敛，Resume 经 `research.schedule` Outbox Message 唤醒新 Worker 并从 checkpoint 恢复。
- 将 Phase 3 已实现的 Tool retry/CB 接入 Task budget 与取消传播；补齐 Node retry、Model fallback 标记和 side-effect 幂等恢复。
- retry exhausted 的分支降级或 Task 失败语义，以及 NodeRun/副作用的重放恢复。

**验收标准**

- Celery 重复交付不产生重复领域执行，Celery result 不作为业务状态来源。
- Research Worker 崩溃后能以新 `lease_token` 从最近成功 checkpoint 恢复。
- Pause、Cancel、Resume 与 Outbox/Recovery 在进程丢失场景下收敛到合法状态。

**下一阶段依赖它什么**

API/SSE 使用本阶段的命令、Outbox、Event 与恢复合同。

### Phase 10：HTTP API 与 SSE

**目标**


**前置依赖**

Phase 1 Auth、Phase 2 State/outbox、Phase 9 Runtime。

**本阶段阅读**

- [api_lifecycle.md](04_platform/api_lifecycle.md)
- [auth_tenant.md](04_platform/auth_tenant.md)
- [datasource.md](02_capabilities/datasource.md) 的管理 API
- [database_write.md](02_capabilities/database_write.md) 的审批 API
- [runtime.md](03_runtime/runtime.md)

**实现**

- Thread/Task/Document/Evidence/Task KG/ModelConfig/DataSource/PendingWrite 资源与命令路由。
- `POST /tasks` 要求 `Idempotency-Key`；`request_hash` 覆盖全部影响初始语义的 CreateTask 字段，Thread/Message/Task/State/checkpoint₀/outbox 同事务。
- 首次创建返回 `201`；同 key+同 hash 返回既有 Task `200`；同 key+不同 hash 返回 `409`；目标 Task 已 soft delete 时返回 `410 idempotency_target_deleted`，原 key/tombstone 继续占用。
- Evidence 列表/详情、Task KG 查询与统一 Workspace 文件读取 API，严格 Ownership/path guard。
- `research_events` 持久化、outbox-dispatcher→Redis live fan-out、SSE `Last-Event-ID` replay/heartbeat；terminal 事件保留 7 天，过旧 cursor 走 snapshot+live，连接数与客户端 buffer 有硬上限。

**验收标准**

- 所有私有路由通过 JWT+Ownership；匿名仅限冻结 Share（Phase 14 实现）。

**下一阶段依赖它什么**


### Phase 11：前端研究工作台

**目标**

在后端 Contract 稳定后实现最小完整产品交互：提问、进度、证据、Workspace、HITL 和报告。

**前置依赖**

Phase 10 API/SSE。

**本阶段阅读**

- [frontend.md](06_product/frontend.md)
- [api_lifecycle.md](04_platform/api_lifecycle.md)
- [report_citation.md](02_capabilities/report_citation.md)

**实现**

- Thread/Task 路由选择、研究输入、三栏布局和资源驱动的四个 Tab。
- SSE `task_updated/export_updated` 缓存失效、断线恢复与 Task snapshot reconcile。
- Evidence/Task KG/Workspace/Document/Report 资源适配器。
- PendingWrite 审批卡、`plan_hash` approve/reject、pause/resume/cancel/replan 交互。
- 知识库 Document 上传与当前 Task XLSX/CSV/Parquet 数据文件上传。
- 空态、部分 branch 失败、任务 failed/cancelled/paused 等错误状态。

**验收标准**

- HITL 可跨浏览器刷新继续审批；按钮状态与后端 status 对齐。
- 完整手工路径：创建任务→观察研究→审批（如有）→查看 final report。

**下一阶段依赖它什么**

完整产品路径可产生真实 Trace/Eval 样本，进入质量体系验收。

### Phase 12：Observability 与 Evaluation

**目标**

把此前各层预留的埋点接成可诊断、可计费、可回归的质量体系。

**前置依赖**

Phase 8 完整 Graph，Phase 9 Runtime，Phase 10 API；Frontend 非 Eval 硬依赖但便于 Trace 检查。

**本阶段阅读**

- [observability.md](05_quality/observability.md)
- [testing.md](05_quality/testing.md)
- [evaluation.md](05_quality/evaluation.md)
- [rag.md](02_capabilities/rag.md) 的检索指标
- [memory.md](02_capabilities/memory.md) 的 Research LTM 指标

**实现**

- OTel 以一次 Worker 激活为 `research.run`，以 handler attempt 为 `graph.node`；统一逻辑操作、CLIENT Span、Token/Cost 与批量落库。
- Evidence/Citation 内容来源链与 Trace 执行链分离，Citation 标识只进入验证失败 Event。
- RAG、Research LTM、E2E EvalRunner、Dataset snapshot、EvalRun config_snapshot 与 Case Result。
- Provider/infra failure 与 model semantic error 分离。
- 工程回归使用确定性时间、ID、Provider attempt 与 Trace Harness；Evaluation Dataset 和 Flywheel Dataset 与测试 Fixture 分开维护。

**验收标准**

- Token/Cost 能按 task/agent/model/provider 聚合，fallback 可辨识。
- 四套 Eval 可复现；RAG 固定 Recall@5，Research LTM 固定 Write Gate Precision/Recall/F1 与 Cross-task Recall@5，E2E 固定事实准确率、引用覆盖率、完成率与 Faithfulness，Skill 固定 Trigger 与 Effect；相同 Dataset、配置快照和代码版本得到可比较结果。

**下一阶段依赖它什么**

Tool Decision Flywheel 的样本 eligibility、治理、质量过滤与 Gate 依赖可信 Trace/Eval。

### Phase 13：Tool Decision Flywheel

**目标**

形成：

`Online Tool Decision → ToolDecisionTrace → Eligibility / Governance → Label Builder → Sample Builder → Immutable Dataset Snapshot → Offline Tool Eval / Safety Gate → QLoRA/SFT → Shadow / Canary → New Tool Model Binding → New ToolDecisionTrace`

闭环。

**前置依赖**

Phase 12 的 Trace、Audit、EvalRun，Phase 3 的 ModelResolver / ToolRegistry，以及现有 bounded ReAct 与 capability_snapshot。

**本阶段阅读**

- [data_flywheel.md](05_quality/data_flywheel.md)
- [evaluation.md](05_quality/evaluation.md)
- [observability.md](05_quality/observability.md)
- [llm.md](04_platform/llm.md)

**实现**

- DeepScout、DataAnalyst、CodeWizard 在 bounded ReAct 中的模型自主 `Tool Call / NO_TOOL` 决策产生 `ToolDecisionTrace`。
- ToolDecisionTrace 保存模型做出决策时实际看到的有界 Context，并复用 Task `capability_snapshot` 恢复当时可见的 Tool Schema。
- Decision、Execution 与 Outcome 分离；Tool 执行成功、HTTP 200 或生成 Evidence 不自动作为正确决策标签。
- Eligibility / Governance / Redaction 先于 Label Builder；正式 Evaluation Gold 与训练数据保持隔离。
- Label Builder 使用 `acceptable_tools[] + argument_assertions`；Sample Builder 保持生产原生 Tool Call / `NO_TOOL` 协议。
- Dataset Snapshot 按 Thread 隔离生成 train / val / test，发布后不可变，并记录 Agent、Tool、difficulty 与 Builder version 等 manifest 信息。
- 建立 Tool Selection Accuracy、Argument Assertion Pass Rate、Tool Decision One-shot Success 与 Safety Gate；Execution Success 单独作为 Tool / Provider 可用性诊断。
- 实现 QLoRA/SFT 训练管线、Shadow orchestration，以及 Task binding freeze 前的 Canary 分流能力，并完成对应自动化测试。
- 数据量与标签质量达到门槛后，再实际执行 `QLoRA/SFT → Offline Gate → Shadow → Canary`。
- 新模型只替换冻结的 Tool Model Binding，不改变 Agent、Graph、ToolRuntime 或 ResearchState 合同。

**验收标准**

- 只有 DeepScout、DataAnalyst、CodeWizard 的模型自主 Tool Decision 产生 ToolDecisionTrace。
- Graph / Runtime 的确定性调用不进入 Tool Decision Dataset。
- `NO_TOOL` 表示当前不再需要继续调用 Tool，不表示 Tool 执行失败后的 fallback。
- Trace Context 与模型决策时真实看到的输入一致，离线阶段不重新摘要或补充未来信息。
- Tool 候选与 Schema 继续复用 capability_snapshot，不建立第二套 Flywheel Tool Snapshot。
- Tool 执行成功不自动充当决策 Gold，正式 Evaluation Gold 不进入训练集。
- 同一 Thread 的全部 Tool Decision 只进入一个 train / val / test split。
- Tool Selection、Argument Assertion、Tool Decision One-shot 与 Safety Gate 分开计算；Execution Success 不污染模型决策质量指标。
- ToolDecisionTrace 独立于 ResearchTask / ResearchState / NodeRun 的业务事务持久化；采集失败不回滚已经完成的业务提交。
- `ToolDecisionTrace → Dataset → Offline Tool Eval / Safety Gate` 实际跑通。
- QLoRA/SFT、Shadow、Canary 的代码路径、合同与自动化测试完成；数据质量未达到门槛时不要求实际训练、Shadow 或 Canary rollout。
- Canary 在 Task Model Binding 冻结前按 eligible Task / traffic 分流；Task 内以及 retry / recovery 期间保持 frozen binding。
- 新模型只替换 Tool Model Binding，不改变 Agent、Graph、Tool 或 ResearchState 合同。

**下一阶段依赖它什么**

Phase 14 可在 Tool Decision 质量门禁具备后完成公开分享、导出和生产交付硬化。

### Phase 14：Share / Export 与生产部署硬化

**目标**

完成可公开交付的冻结分享、导出 Job 和生产运行/备份/恢复。

**前置依赖**

Phase 10 API、Phase 11 UI、Phase 12 观测；Export 使用 Phase 9 粗粒度队列。

**本阶段阅读**

- [share_export.md](06_product/share_export.md)
- [deployment.md](04_platform/deployment.md)
- [auth_security.md](04_platform/auth_security.md)
- [storage.md](04_platform/storage.md)

**实现**

- final report Share 冻结为自包含公开 Snapshot；Export Job 生成 DOCX/PDF；Snapshot、Asset 与导出文件统一走 Workspace finalize/quota。
- 固定版本多目标镜像、research/ingestion/export 三队列、outbox-dispatcher、Sandbox Runner 与职责化 readiness。
- Nginx 分离普通 API、SSE 与 Document 上传配置；生产 Secret、独立 Migration Job 和资源限额。
- `documents` / `workspaces` 两个私有 Bucket；PG(+pgvector 研究记忆)/MinIO/Secret 备份；ES、Redis 与 Task KG 重建演练；CI/CD 与回滚。
- 恢复目标：整体 `RPO≤6h`、PG+MinIO core recovery `RTO≤8h`、完整应用 `RTO≤24h`；每月实际 restore drill。

**验收标准**

- Share 只冻结固定 final report version；Export 相同幂等键和请求返回同一逻辑记录，Worker attempt fencing 可隔离迟到执行。
- Public Token 只解析冻结 Snapshot 与 manifest Assets；Task 删除同步撤销 Share，文件随后由 Workspace GC 清理。

**下一阶段依赖它什么**


## 7. Task Research KG


- [build_knowledge_graph.md](02_capabilities/tools/knowledge_graph/build_knowledge_graph.md)
- [query_knowledge_graph.md](02_capabilities/tools/knowledge_graph/query_knowledge_graph.md)

Task KG 是正式的按需能力：从当前 Task 的不可变 Evidence 构建，所有 Claim 与 Relation 通过 `evidence_id` 回源。Neo4j 保存派生关系索引，不替代 Document、Workspace、Evidence 或 Research LTM。

**实现**

- `KnowledgeGraphRepository` 以 tenant/task namespace 隔离 Neo4j 数据；父级 Task 决定 owner 授权。
- `build_knowledge_graph` 以当前 query、hypotheses 和 Evidence 完成 Entity、Claim、Provenance、Entity Resolution 和增量 Upsert；Replan 后按新问题重建。
- `query_knowledge_graph` 提供有界 neighbors/path/search，并返回 Claim 与 `evidence_id`。

**验收标准**

- 不同 Task 的图数据完全隔离。
- 每个 grounded Claim/Relation 均可通过 `evidence_id` 回到当前 Task Evidence。
- 删除 Neo4j Task namespace 后可从 Evidence 重建，且不会影响 Document、Workspace 或 Research LTM。


## 8. 完成定义

整个项目只有在以下条件同时满足时才算实现完成：

- Phase 0→14 的阶段验收均有自动化证据或明确的部署演练记录。
- Task Research KG 的构建、查询、隔离、回源和重建验收通过。
- 固定参数（RAG 融合权重、Tool hard limits、Sandbox 资源上限、Chart/Table 限额等）在对应 SoT、Settings 与契约测试中保持一致。
- README、代码、迁移、API schema、Prompt 和部署配置引用同一套 Contract。
