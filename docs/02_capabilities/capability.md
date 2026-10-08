# DeepResearch-mutilagent：Capability / Skill 扩展

## 0. 定位

```text
Expert Agent
  → Skill Runtime 提供任务方法与工作流指导
  → Agent 根据 Tool Policy 选择原子 Tool
  → ToolRuntime 执行 Tool
  → LocalToolAdapter 或 MCPToolAdapter
  → ToolResult
       ├─ 短结果：inline data
       └─ 长结果或文件：Workspace + workspace_path
  → Graph MergeEvidence
  → evidence_pool
```

| 层 | 合同 | 职责 |
|---|---|---|
| Skill | metadata + `SKILL.md` + `required_tools` | 提供方法、步骤和领域工作流 |
| Tool | name + schema + execute + permission | 提供原子执行能力 |
| ToolRuntime | 统一执行入口 | schema、资源解析、deadline、adapter、结果归一化与观测 |
| Adapter | Local 或 MCP | 调用本地实现或外部 Tool Provider |

Workspace 是当前 Task 的外置文件系统，保存检索工具返回的长内容、Compute 生成文件和报告。`evidence_pool` 是 Graph 合流后的证据卡片集合，保存完整短结果、摘要以及指向 Workspace 或 Document 的引用。

---

## 1. 组件与 Task 能力快照

Research Graph、六个 Expert Agent 和 ResearchState 核心合同保持固定；Skill、Tool 与 Provider 通过 Registry 装配。

```text
SkillLoader
  → 解析 metadata、SKILL.md 和关联资源

SkillRegistry
  → 按 Agent Skill whitelist 暴露 Skill metadata

ToolRegistry
  → 保存 Local / MCP Tool 的规范化合同

ToolRuntime
  → 使用 Agent Tool Policy 的授权结果
  → 校验参数与资源范围
  → 调用对应 Adapter
  → 归一化 ToolResult、错误和 Trace
```

Task 创建时冻结一份能力快照，并保存在 ResearchTask 执行元数据中：

```text
capability_snapshot:
  skills[]:
    - name
    - content_hash

  tools[]:
    - name
    - schema_hash
    - provider_ref

  policy_revision
```

Skill 激活、Tool 调用与任务恢复统一读取 `capability_snapshot`。Provider 的凭据以 `credential_ref` 管理，调用时由 CredentialResolver 解析。`provider_ref` 指向不可变的 Provider 注册版本，其中包含 Adapter 类型和配置引用；Registry 更新时生成新引用，运行中的 Task 继续使用快照内的旧引用。

`content_hash` 与 `schema_hash` 同时承担冻结版本的解析身份，不只是校验值。SkillLoader 与 ToolRegistry 必须能够按快照中的 `name + content_hash` 或 `name + schema_hash + provider_ref` 解析原不可变合同；Task 恢复不按 name 加载 Registry latest。Registry 更新产生新不可变版本，旧版本至少保留到引用它的 Task 生命周期结束。

---

## 2. Skill Runtime

### 2.1 渐进式加载

```text
L1：向当前 Agent 提供 Skill name、description 和 required_tools
  ↓ Agent 选择 activate
L2：SkillRuntime 调用 SkillLoader 加载完整 SKILL.md
  ↓ 当前任务确有需要
L3：SkillRuntime 按需加载 references / scripts / assets
```

SkillRuntime 负责激活流程，SkillLoader 负责读取 SKILL.md 和关联资源。

L3 的 `references / scripts / assets` 都是 Skill 指导资源。SkillRuntime 只加载并向 Agent 提供这些资源，不直接执行 `scripts`；需要运行脚本时，Agent 必须调用 capability snapshot 与 Tool Policy 已授权的 Tool（例如 `sandbox_execute`），并继续经过 ToolRuntime、Sandbox、deadline、budget 与 audit 链。

### 2.2 Agent Skill whitelist

```yaml
# agents/data_analyst.yaml
skills:
  - data-analysis

# agents/code_wizard.yaml
skills:
  - visualization

# skills/visualization/SKILL.md frontmatter
name: visualization
description: >
  当任务需要将研究数据转为折线、柱状、饼图或散点图，
  或比较趋势、分布与类别差异时使用。
required_tools:
  - generate_chart
  - render_table
```

### 2.3 激活流程

```text
启动服务
  → SkillLoader 扫描并校验 skills/*
  → SkillRegistry 保存 metadata

创建 Task
  → 根据 Agent whitelist、Tool Policy 与当前 Registry 生成 capability_snapshot

执行节点
  → Agent 读取快照内可见 Skill 的 L1 metadata
  → 选择 Skill
  → 校验 required_tools 包含在 capability_snapshot.tools 与 Agent Tool Policy 中
  → SkillRuntime 加载 SKILL.md 和所需资源
  → Agent 按 Skill 指导调用 Tool
```

示例：

```text
分析 sales.xlsx 并绘制地区增长趋势
  → DataAnalyst 激活 data-analysis
  → inspect_dataset / profile_dataset / query_dataset
  → adopted SQL branch results / DatasetRef → MergeEvidence
  → Post-Merge Analysis Decision 生成 AnalysisRequest
  → CodeWizard 激活 visualization → generate_chart
  → 图表进入 Workspace 和 Compute branch，MergeEvidence 创建回指输入 Evidence 的 Derived Evidence

分析 ERP 近六个月供应商交付趋势
  → DataAnalyst 激活 data-analysis
  → query_supplier_data（MCP）
  → MergeEvidence → AnalysisRequest
  → CodeWizard 激活 visualization → generate_chart（Local）
```

---

## 3. ToolRuntime 与 Adapter

Agent Tool Policy / allowlist 是 Tool 授权来源。ToolRuntime 使用该授权结果，并统一完成执行期工作：

```text
Tool request
  → ToolRegistry lookup
  → Agent Tool Policy
  → input schema validation
  → ResourceResolver
  → deadline / cancellation / idempotency
  → LocalToolAdapter 或 MCPToolAdapter
  → ToolResult normalization
  → Trace / metrics
```

### 3.1 LocalToolAdapter

```text
ToolRegistry
  → LocalToolAdapter
       ├─ Dataset / SQL
       ├─ Chart / Table
       ├─ Sandbox / File
       └─ Browser / Parser
```

### 3.2 MCPToolAdapter

```text
注册 MCP endpoint
  → EndpointPolicy + SSRFGuard.validate(endpoint)
  → Adapter 按需 CredentialResolver.resolve(credential_ref)
  → 官方 MCP Client/SDK 建连
  → tools/list
  → 规范化 name / description / JSON Schema
  → ToolRegistry（provider_type=mcp）
  → 创建 Task 时写入 capability_snapshot

调用 MCP Tool
  → 从 capability_snapshot 取得 Tool 合同与 provider_ref
  → ToolRuntime
  → tools/call
  → 统一 ToolResult / error / OTel
```

`SSRFGuard` 校验 endpoint 的 scheme、DNS/IP、实际连接地址和重定向目标；资源授权通过后，MCP Adapter 才按需解析 `credential_ref`。Registry key 使用稳定的 server identity + tool name。

### 3.3 ToolAdapter 合同

```text
ToolAdapter.execute(
  ToolExecutionContext,
  normalized_arguments
) → ToolResult
```

ToolRuntime 负责 schema、授权、资源范围、deadline、retry/Circuit Breaker 和观测；Adapter 只负责协议调用与 Provider 响应转换。新增 Adapter 通过 Registry 注册并返回统一 ToolResult/ToolError，不在 Graph 或 Agent 中增加 Provider 分支。

Tool 合同、Tool 清单与 Agent allowlist 见 [`tool.md`](tool.md)。数据库写路径见 [`database_write.md`](database_write.md)。

---

## 4. Skill 与数据能力目录

| Skill | required_tools / 行为 |
|---|---|
| `data-analysis` | SQL 与 Dataset inspect/profile/query，输出 adopted SQL branch results / DatasetRef |
| `visualization` | `generate_chart` + `render_table` |
| `rendered-web` | `render_page` + Web Tools |
| `market-research` | Web、RAG 与结构化数据研究流程 |
| `policy-analysis` | 政策检索、证据整理与报告工作流 |
| `knowledge-graph` | Agent 使用 `query_knowledge_graph`；Graph 派生流程使用 `build_knowledge_graph` |

`generate_chart` 支持 `line | bar | pie | scatter`。Task Knowledge Graph 是正式能力；是否增加 `network` 图表类型与 KG 构建/查询解耦。

`knowledge-graph` 及其 Tool 只在 Task KG 专项阶段启用；Phase 7 创建的 capability snapshot 不包含该 Skill 或 `build_knowledge_graph / query_knowledge_graph`。

Hypothesis Plan 使用 `queries[channel=sql]` 表达结构化数据问题：

```text
DataAnalyst 接收当前研究轮次的 SQL queries
  → 业务数据库资源
       → inspect_schema / sample_table / execute_readonly_sql
       → DataSource grant / policy / credential

  → XLSX / CSV / Parquet Dataset
       → inspect_dataset / profile_dataset / query_dataset
       → SQL/Data 模块解析、Dataset scope 与 DuckDB 临时只读 View
```

业务数据库与 Dataset 共用 DataAnalyst 和 SQL Graph 分支；Dataset 只作为 SQL/Data 能力的内部资源类型，不形成独立 Evidence 来源。完整短结果直接返回，长结果与生成文件写入 Workspace。

---

## 5. 专家绑定

| Agent | Skill whitelist | Tool / 能力面 |
|---|---|---|
| ChiefArchitect | 研究规划类 Skill | Direct、Plan、Deepen、Replan；无业务 Tool |
| DeepScout | `rendered-web`、`market-research`；Task KG 专项阶段增加 `knowledge-graph` | Web、Document RAG；专项阶段增加 Task KG |
| DataAnalyst | `data-analysis`、领域 Skill | SQL/Dataset、PrepareWrite；只输出 adopted SQL branch results / DatasetRef |
| CodeWizard | `visualization` | sandbox、chart/table |
| LeadWriter | 写作类 Skill | evidence_pool 引用与报告管线 |
| CriticMaster | 评审类 Skill | Writer 前审核 query、hypotheses 与 evidence_pool |

Skill 指导 Agent 如何组合能力；Agent Tool Policy 决定当前 Agent 可调用的 Tool；ToolRuntime 执行已授权调用。

Skill 是否正确触发以及触发后是否改善结果由 [`evaluation.md`](../05_quality/evaluation.md) 的 Skill Suite 评估；生产 Trace 只记录激活事实，不以“被调用过”代替效果评价。

---

## 6. 遥测

```text
capability_type: web | rag | sql | chart | render | code | kg | workspace
provider_type:   local | mcp

task_id
capability_snapshot.policy_revision
skill.name?
tool.name?
tool.schema_hash?
provider_ref?
```

示例：`query_supplier_data` 使用 `capability_type=sql`、`provider_type=mcp`。

---

## 7. 验收标准

```text
□ Skill、Tool、ToolRuntime 与 Adapter 职责清晰
□ SkillRuntime 完成 L1 metadata、SKILL.md 和关联资源的渐进式加载
□ Agent Skill whitelist 与 Tool Policy 分别管理 Skill 可见性和 Tool 授权
□ Task 创建时生成统一 capability_snapshot，恢复时复用同一快照
□ SkillLoader / ToolRegistry 可按快照 hash 解析被冻结版本，恢复不读取 latest
□ Skill scripts 只作为指导资源加载；实际执行继续经过已授权 Tool 与 ToolRuntime
□ ToolRuntime 统一完成 schema、资源解析、deadline、adapter、归一化与观测
□ 所有 Adapter 实现统一 execute 合同；新增 Provider 不修改 Graph 或 Agent
□ provider_ref 指向不可变注册版本，运行中 Task 复用快照锁定的 Adapter 与配置
□ MCP endpoint 经过 SSRFGuard，credential_ref 经过 CredentialResolver
□ Knowledge Graph 是专项阶段启用的正式按需能力，所有 Claim 通过 evidence_id 回到原 Evidence
□ Dataset 归入 DataAnalyst / SQL 分支，并在 SQL/Data Tool 内处理
□ Web / SQL 的完整短结果直接返回，长结果与 Compute/报告文件写入 Workspace
□ Web、Document、SQL 与 Compute ToolResult 经 MergeEvidence 形成 evidence_pool；KG 与数据库写路径不新增 Evidence
□ 数据库写入使用 WritePlan → HITL → WriteExecutor
□ Skill Trigger 与 Effect 使用独立评测口径，不与 Route / Tool Decision 训练集混合
```

---

## 8. 实现顺序

> 全局 Phase 顺序见 [`docs/README.md`](../README.md)。

```text
P0 / Phase 7 当前主链
  SkillLoader + SkillRegistry + SkillRuntime
  data-analysis / visualization / rendered-web
  market-research / policy-analysis
  Agent Skill whitelist + Tool Policy
  ToolRegistry + ToolRuntime + LocalToolAdapter
  MCPToolAdapter + 一个外部 MCP Server PoC
  SSRFGuard + CredentialResolver
  capability_snapshot
  capability_type × provider_type telemetry

Task KG 专项阶段
  knowledge-graph
  build_knowledge_graph / query_knowledge_graph

后续可选
  generate_chart 增加 network 可视化
```

同步合同：[`tool.md`](tool.md)、[`research_workspace.md`](research_workspace.md)、[`data_flywheel.md`](../05_quality/data_flywheel.md)。

## 附录：最小测试集

```text
□ Skill metadata → SKILL.md → 按需资源加载
□ DataAnalyst 仅可见 data-analysis；CodeWizard 可见 visualization
□ required_tools 与 capability_snapshot.tools、Agent Tool Policy 一致
□ capability_snapshot 可按 content_hash / schema_hash 恢复旧 Skill 与 Tool 合同
□ Skill scripts 的执行请求经过已授权 Tool，不形成 SkillRuntime 旁路
□ Local 与 MCP Tool 经同一 ToolRuntime 返回 ToolResult
□ 新增 Fake Adapter 可只通过 Registry 注册接入，无需修改 Graph/Agent 分支
□ SSRF endpoint 校验与 credential scope 校验分别覆盖
□ MCP Registry 变化后，运行中 Task 继续复用 capability_snapshot
□ SQL 与 Dataset 在统一 SQL 分支下选择正确 Tool 与资源策略
□ Phase 7 capability snapshot 不包含 knowledge-graph 或 Task KG Tool
```
