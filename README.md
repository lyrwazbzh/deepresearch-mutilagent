# DeepResearch Multi-Agent

> 基于 FastAPI、LangGraph 与 React 构建的证据驱动多 Agent 深度研究系统。
> 系统通过 `direct / focused / deep` 三档路由，在 Web、知识库、业务数据库与本地数据集之间协同研究，并以可追溯 Evidence、Citation 和可恢复运行时保证结果质量。

## 项目简介

DeepResearch Multi-Agent 面向需要多来源检索、数据分析、受控计算和可审计报告的复杂研究任务。它不是一个让多个 Agent 自由对话的 Demo，而是一套由状态机控制的研究工作流：每一步都有明确输入输出、工具权限、预算、检查点和质量门禁。

项目当前代码与测试已覆盖路线图 Phase 1–14 的主体能力，包括：

- `direct / focused / deep` 三档任务路由；
- Web、Document RAG、SQL / Dataset 三路研究；
- 假设驱动的 Plan、Coverage 与 Critic 闭环；
- Sandbox 计算、图表与表格产物；
- Evidence Pipeline、ReportAst 与 Citation 校验；
- Research LTM 与当前任务 Task KG；
- 数据库写入 HITL 审批；
- Checkpoint、Outbox、Lease、幂等与断点恢复；
- Auth、多租户、SSE、前端研究工作台；
- OpenTelemetry、Evaluation 与 Tool Decision Flywheel；
- Share、Export 与生产部署基线。

本地开发默认使用确定性的 Fake Model Provider，可在没有外部模型账户的情况下运行主要流程。真实模型、Web Provider、训练和生产发布需要按环境显式配置；仓库不会把“训练管线已实现”表述成“已经完成生产训练”。

## 系统架构

```text
                            ┌──────────────────────┐
        Browser ──────────▶ │ React + Vite :5173  │
                            └──────────┬───────────┘
                                       │ /api
                            ┌──────────▼───────────┐
                            │ FastAPI API :8000    │
                            └──┬────┬────┬────┬────┘
                               │    │    │    │
                ┌──────────────┘    │    │    └──────────────┐
                ▼                   ▼    ▼                   ▼
       ┌────────────────┐  ┌───────────────┐  ┌──────────┐  ┌──────────┐
       │ PostgreSQL     │  │ Elasticsearch │  │  MinIO   │  │  Redis   │
       │ State/CP/Outbox│  │ BM25 + Vector │  │ Objects  │  │Broker/SSE│
       │ Audit/LTM      │  │ Document RAG  │  │Workspace │  │  Cache   │
       └───────┬────────┘  └───────────────┘  └──────────┘  └────┬─────┘
               │                                                   │
               │                                      ┌────────────▼───────────┐
               │                                      │ Celery Workers         │
               │                                      │ research / ingestion   │
               │                                      │ export                  │
               │                                      └────────────┬───────────┘
               │                                                   │
               │                                      ┌────────────▼───────────┐
               └─────────────────────────────────────▶│ LangGraph Runtime      │
                                                      │ ModelRuntime/ToolRuntime│
                                                      └──────┬──────────┬───────┘
                                                             │          │
                                                   ┌─────────▼───┐  ┌───▼─────────┐
                                                   │ DataSource  │  │ Sandbox     │
                                                   │ Web/Models  │  │ Docker/runsc│
                                                   └─────────────┘  └─────────────┘

       Neo4j（task-kg profile）保存当前 ResearchTask 的可重建派生关系索引，
       不替代 PostgreSQL 中的 Evidence，也不是 Research 主链的硬依赖。
```

生产部署由 Nginx 统一承接前端、API、SSE 与下载流量；本地开发可直接运行 Vite 和 Uvicorn。完整部署合同见 [deployment.md](docs/04_platform/deployment.md)。

## 研究工作流

### 三档入口

```text
User Query
    │
    ▼
RouteNode
    ├─ direct
    │    └─ ChiefArchitect
    │         └─ 无外部 Tool 的普通问答、解释与对话
    │
    ├─ focused
    │    └─ 选择一个执行 Agent
    │         ├─ DeepScout   → Web / RAG / 文档读取
    │         ├─ DataAnalyst → SQL / Dataset
    │         └─ CodeWizard  → Compute / Sandbox
    │
    └─ deep
         └─ 进入完整深度研究 DAG
```

路由同时考虑是否需要外部 Tool 与任务复杂度。ChiefArchitect 不持有研究 Tool；简单但需要 Tool 的问题直接进入对应的 focused Worker。

### Deep Research DAG

```text
Query Completion
        │
        ▼
ChiefArchitect: Plan
        │
        ├────────────┬────────────┐
        ▼            ▼            ▼
   Web Search    Document RAG   SQL / Dataset
   DeepScout     DeepScout      DataAnalyst
        └────────────┴────────────┘
                     │
                     ▼
               MergeEvidence
                     │
                     ▼
          Post-Merge Analysis Decision
             ├─ no compute ────────────┐
             └─ AnalysisRequest        │
                    │                  │
                    ▼                  │
               CodeWizard             │
                    │                  │
                    └──── MergeEvidence
                              │
                              ▼
                         CoverageGate
                    ┌─────────┴─────────┐
              incomplete             complete
                    │                   │
                    ▼                   ▼
        ChiefArchitect: Deepen     CriticMaster
                    │          ┌────────┴─────────┐
                    └──────────┤ retrieve/deepen │
                               └────────┬─────────┘
                                      pass
                                       │
                                       ▼
                              DatabaseWriteGate
                              ├─ WriteIntent → HITL
                              └─ no write
                                       │
                                       ▼
                                  LeadWriter
                                       │
                                       ▼
                              ValidateReport
                              ├─ invalid → Revise
                              └─ valid   → Finalize
```

`CoverageGate` 只检查结构完整性；`CriticMaster` 判断证据在语义上是否足以支持或反驳假设。报告完成后不再重新启动研究，结构和引用问题由确定性的 `ValidateReport → Revise` 环处理。

## 六个专家 Agent

| Agent | 核心职责 | Tool 边界 |
| --- | --- | --- |
| ChiefArchitect | Direct Answer、Plan、Deepen、Replan | 不调用外部研究 Tool |
| DeepScout | Web、论文、Document RAG、Task KG 查询与多跳溯源 | Web / Document / KG Query |
| DataAnalyst | Schema 理解、Text2SQL、Dataset / DuckDB 分析 | SQL / Data Tools |
| CodeWizard | 执行 AnalysisRequest，完成受控计算和可视化 | Compute / Sandbox Tools |
| CriticMaster | Writer 前的证据语义充分性审核 | 无开放 Tool Loop |
| LeadWriter | 基于审核通过 Evidence 生成 ReportAst | 无开放 Tool Loop |

只有 DeepScout、DataAnalyst、CodeWizard 运行有界 ReAct。ChiefArchitect、CriticMaster 和 LeadWriter使用一次结构化调用，避免无限自治和职责漂移。

## 核心数据链

```text
ToolResult
    ↓
Agent 采纳并解释
    ↓
research_rounds[depth].web / rag / sql / compute
    ↓
MergeEvidence
    ↓
evidence_pool
    ↓
Coverage / Critic / ReportAst / Citation
```

关键边界：

- `SearchHit`、Chunk、SQL Row 和 Compute File 都不是正式 Evidence；
- 执行 Agent 只能提交 adopted branch result，只有 `MergeEvidence` 能写 `evidence_pool`；
- Evidence 绑定具体 Hypothesis，并通过稳定来源标识和 `content_hash` 保留 provenance；
- Task KG 是 Evidence 的派生关系视图，不形成第二事实源；
- Citation 只引用 `evidence_id`，不复制来源元数据；
- ReportAst 是报告内容事实源，Markdown / HTML / DOCX / PDF 是确定性投影。

## 状态、存储与恢复

| 对象 | 职责 |
| --- | --- |
| ResearchTask | 运行生命周期、status、lease、budget、pause/resume/cancel |
| ResearchState | LangGraph 研究业务状态，不保存运行时 task status |
| PostgreSQL | 权威结构化事实源：Task、State Snapshot、Checkpoint、NodeRun、Outbox、Audit、LTM |
| MinIO | Document 原文件、Workspace 长内容、Compute Bundle、Report、Export |
| Elasticsearch | 可重建的 Document Chunk 全文与向量检索副本 |
| Neo4j | 可由 Evidence 重建的 Task KG 派生索引 |
| Redis | Celery Broker、SSE live fan-out、Semantic Cache；不是业务状态源 |

恢复链路由 Checkpoint、NodeRun、稳定 `operation_key`、Lease Fencing 和 Outbox 共同保证。Celery 只提供粗粒度 at-least-once 投递，任务真实状态始终从 PostgreSQL 恢复。

## 核心能力

### 检索与数据分析

- Web Search：Tavily / Jina Search，论文检索支持 arXiv 与 Web fallback；
- 网页读取：Jina Reader，动态页面由 DeepScout 显式降级到 `render_page`；
- Hybrid RAG：BM25 + kNN + RRF + Rerank + Parent Expansion；
- 外部 PostgreSQL：Schema 检查、受控采样、AST / lineage 校验后的只读 SQL；
- Task Dataset：CSV / XLSX / Parquet 经 Workspace 和 Dataset Catalog 交给 DuckDB；
- Query Cache：规范化查询、租户隔离、TTL 与 Provider 维度缓存；
- 来源治理：URL / DOI 归一化、近重复去重、重排、来源质量和 stance 识别。

### Compute 与报告

- CodeWizard 只消费显式 `AnalysisRequest`；
- Docker / gVisor Sandbox 默认无网络、无业务数据库凭据；
- `/inputs` 只读，`/outputs` 是唯一可写产物目录；
- `sandbox_execute`、`generate_chart`、`render_table` 统一写入 Workspace Compute Bundle；
- Manifest 保留代码、Evidence / Dataset 输入、输出文件和 hash 的完整血缘；
- LeadWriter 生成 ReportAst，CitationValidator 确定性验证引用完整性；
- Share 使用冻结 Snapshot，Export 固定 `report_version` 并异步生成产物。

### 记忆、质量与治理

- Query Completion 使用近期消息、会话摘要、Research LTM 和资源范围；
- Research LTM 存储在 PostgreSQL + pgvector，按 tenant / user 隔离；
- Task KG 只查询当前任务的 Entity、Claim、Relation 和 Evidence provenance；
- Prometheus / Grafana、OpenTelemetry / Jaeger、结构化日志与 Audit 分层；
- RAG、Research LTM、E2E Research、Skill 四套 Evaluation Suite；
- Tool Decision Flywheel 只训练执行 Agent 的 `Tool Call / NO_TOOL` 决策，不训练 RouteNode；
- Dataset Snapshot、Offline Gate、QLoRA / SFT、Shadow 与 Canary 均保持版本化和 Task Binding 冻结。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| Frontend | React 19、TypeScript 6、Vite、TanStack Query、React Router |
| API | Python 3.11+、FastAPI、Pydantic Settings |
| Agent Graph | LangGraph、结构化输出、bounded ReAct |
| Async Runtime | Celery、Redis、Transactional Outbox |
| Database | PostgreSQL、SQLAlchemy、Alembic、pgvector |
| Search | Elasticsearch、IK Analyzer、BM25、Dense Vector、RRF |
| Object Storage | MinIO |
| Data / Compute | DuckDB、Pandas、PyArrow、Docker、gVisor/runsc |
| Knowledge Graph | Neo4j（`task-kg` profile） |
| Observability | OpenTelemetry、Prometheus、Grafana、Jaeger、结构化日志 |
| Testing | Pytest、Testcontainers、Playwright、Ruff、Mypy |

## 项目结构

```text
deepresearch-mutilagent/
├── src/
│   ├── agent/               # 主 Agent、Subagents、Runtime、Tools 与 Backends
│   ├── api_view/            # FastAPI、Auth、SSE 与管理接口
│   ├── skills/              # Agent Skill 资源
│   └── test/                # Unit / Contract / Integration / E2E / Acceptance
├── frontend/                # React 研究工作台与管理页面
├── docs/                    # 架构与开发合同 Source of Truth
├── migrations/              # Alembic migrations
├── ops/                     # Nginx、备份恢复与生产部署配置
├── compose.yaml             # 本地基础设施
├── compose.production.yaml  # 生产拓扑
└── pyproject.toml
```

## 快速开始

### 1. 前置要求

- Python 3.11+
- Docker 与 Docker Compose
- Node.js 22+
- pnpm 11+

如需真实 Sandbox 隔离，主机还需要安装并配置 gVisor `runsc`；不运行 Compute 场景时可先完成其余本地流程。

### 2. 安装后端依赖

```powershell
cd "D:\Autumn recruitment\deepresearch-mutilagent"
Copy-Item .env.example .env

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

本地纯 HTTP 开发时，将 `.env` 中的以下配置设为：

```dotenv
DEEPRESEARCH_MODEL_MODE=fake
AUTH_COOKIE_SECURE=0
```

Fake 模式只用于本地开发和确定性测试。生产环境必须配置稳定的 JWT 私钥、Cursor Signing Key、数据库与对象存储密码，并启用 Secure Cookie。

### 3. 启动基础设施

```powershell
docker compose --profile task-kg up -d postgres redis minio elasticsearch neo4j
```

不需要 Task KG 时可以省略 `--profile task-kg` 和 `neo4j`：

```powershell
docker compose up -d postgres redis minio elasticsearch
```

默认本地端口：

| 服务 | 地址 |
| --- | --- |
| PostgreSQL | `localhost:5432` |
| Redis | `localhost:6379` |
| MinIO API / Console | `localhost:9000` / `localhost:9001` |
| Elasticsearch | `localhost:9200` |
| Neo4j HTTP / Bolt | `localhost:7474` / `localhost:7687` |

### 4. 初始化数据库、模型配置与管理员

```powershell
python -m alembic upgrade head
python -m src.agent.bootstrap

$env:PHASE10_BOOTSTRAP_PASSWORD='ChangeMe-Local-123!'
python -m src.api_view.bootstrap_auth --email admin@example.com --role admin
Remove-Item Env:PHASE10_BOOTSTRAP_PASSWORD
```

Bootstrap 密码至少 12 位，仅通过环境变量注入，不要写入仓库或日志。

### 5. 启动后端进程

分别打开三个 PowerShell 窗口：

```powershell
# API
python -m uvicorn src.api_view.web_main:app --host 0.0.0.0 --port 8000
```

```powershell
# Celery Workers
python -m celery -A src.agent.workers.celery_app:celery_app worker -Q research,ingestion,export --concurrency=3
```

```powershell
# Transactional Outbox Dispatcher
python -m src.agent.workers.dispatcher_main
```

### 6. 启动前端

```powershell
cd frontend
pnpm install
pnpm dev
```

访问：

- 前端工作台：<http://localhost:5173>
- OpenAPI：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>
- Readiness：<http://localhost:8000/readyz>

使用刚才创建的管理员邮箱和密码登录。Refresh Credential 由 HttpOnly Cookie 保存，Access Token 只保存在前端运行时。

## 配置真实 Provider

本地 Fake Provider 不会访问互联网或真实模型。接入真实能力时至少需要：

1. 将 `DEEPRESEARCH_MODEL_MODE` 改为 `configured`；
2. 配置 OpenAI-compatible Model Provider 与 API Key；
3. 按需配置 Tavily、Jina、arXiv、Elasticsearch、DataSource 和 Sandbox；
4. 重新运行 `python -m src.agent.bootstrap` 生成或更新 ModelConfig / Role Binding；
5. 通过前端“系统设置 → 模型配置”或 API 完成连接测试。

所有 Provider Secret 只保存引用或加密值，不进入 Prompt、ResearchState、Tool Trace 或前端回显。

## 验证与测试

### 后端

```powershell
python -m ruff check src migrations ops
python -m mypy src/agent src/api_view
python -m pytest
```

### 前端

```powershell
cd frontend
pnpm quality
pnpm e2e
```

### 真实基础设施验收

仓库提供按实际能力命名的验收与 Smoke 脚本，例如：

```powershell
python src/test/acceptance/runtime_kg_sandbox_acceptance.py
python src/test/acceptance/http_auth_acceptance.py
python src/test/smoke/observability_evaluation_smoke.py
python src/test/smoke/tool_flywheel_smoke.py
python src/test/acceptance/share_export_acceptance.py
```

这些脚本要求相应基础设施和服务已启动；真实 Provider 验收还需要有效凭据。详细矩阵见 [testing.md](docs/05_quality/testing.md)。

## API 概览

FastAPI OpenAPI 是具体请求/响应合同的权威入口。主要资源包括：

- Auth：Login、Refresh、Logout、Session Revoke；
- Thread / Task：创建、查询、pause、resume、cancel、replan；
- SSE：支持 `Last-Event-ID`、cursor 过期恢复与 snapshot reconcile；
- Evidence / Report / Citation / Workspace；
- Document、Data File、DataSource、ModelConfig；
- Research LTM 与 Task Knowledge Graph；
- PendingWrite 审批与执行结果；
- Share Snapshot、Export Job 与下载授权。

完整生命周期见 [api_lifecycle.md](docs/01_architecture/api_lifecycle.md)。

## 文档导航

根 README 负责项目入口，`docs/` 是架构与开发合同的 Source of Truth。开始修改代码前，建议先阅读 [docs/README.md](docs/README.md)。

| 主题 | 文档 |
| --- | --- |
| 系统边界与 DAG | [architecture.md](docs/01_architecture/architecture.md)、[graph.md](docs/01_architecture/graph.md) |
| State 与 Evidence | [state.md](docs/01_architecture/state.md)、[evidence_pipeline.md](docs/01_architecture/evidence_pipeline.md) |
| Agent 职责 | [agents.md](docs/01_architecture/agents.md) |
| Runtime 与恢复 | [runtime.md](docs/01_architecture/runtime.md) |
| Tool / Skill / Harness | [tool.md](docs/02_capabilities/tool.md)、[capability.md](docs/02_capabilities/capability.md) |
| Web / RAG / SQL / Compute | [docs/02_capabilities](docs/02_capabilities) |
| Workspace 与 Document | [research_workspace.md](docs/03_data/research_workspace.md)、[document_system.md](docs/03_data/document_system.md) |
| 存储与部署 | [storage.md](docs/04_platform/storage.md)、[deployment.md](docs/04_platform/deployment.md) |
| Auth 与多租户 | [auth_security.md](docs/04_platform/auth_security.md)、[auth_tenant.md](docs/04_platform/auth_tenant.md) |
| Observability / Eval / Flywheel | [docs/05_quality](docs/05_quality) |
| 前端 | [frontend.md](docs/06_frontend/frontend.md) |
| Share / Export | [share_export.md](docs/07_delivery/share_export.md) |

## 设计原则

1. **质量由控制流保证**：Coverage、Critic、Citation Validation 和预算门禁不是模型自觉行为。
2. **业务状态只有一个权威源**：PostgreSQL 保存权威事实；ES、Neo4j、Redis 均可重建或可丢失。
3. **Agent 决策与 Tool 执行分离**：Agent 选择能力，ToolRuntime 负责 Schema、Policy、Deadline、Cancel 和幂等。
4. **证据先于报告**：Writer 只消费审核通过的 Evidence，不在写作阶段重新研究。
5. **恢复优先于理想路径**：每个副作用都必须考虑重放、重复投递、Worker crash 和部分失败。
6. **长内容不进入 Graph State**：ResearchState 保存短结构化状态，正文和文件进入 Document / Workspace。
7. **安全边界不下放给模型**：多租户、Secret、SSRF、SQL Policy、Sandbox 与 HITL 都由确定性组件执行。

## 生产注意事项

- 不要在生产环境使用自动生成的临时 JWT Key 或示例 Secret；
- 保持 `AUTH_COOKIE_SECURE=1`，并通过 HTTPS 暴露前端和 API；
- 外部 URL 必须经过 SSRFGuard、DNS/IP 校验与 egress policy；
- 外部数据库读写使用独立 Principal，写入必须经过 PendingWrite + HITL；
- Sandbox 不得持有 PostgreSQL、Redis、MinIO、Elasticsearch 或 Provider Secret；
- Neo4j 是 Task KG 派生索引，故障时不得阻塞 Research 主链；
- Presigned URL 使用短 TTL，Task 删除后立即停止签发新的下载授权；
- Tool Decision 训练、Shadow 和 Canary 只有在受治理标签和 Offline Gate 达标后才能真实运行。

---

如果你准备参与开发，请从 [docs/README.md](docs/README.md) 的 Source of Truth 与 Phase 顺序开始，而不是仅凭目录或旧代码推断架构。
