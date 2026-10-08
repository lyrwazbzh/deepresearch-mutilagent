# DeepResearch-mutilagent：六个专家 Agent

## 0. 定位

项目固定包含六个 Agent：

```text
ChiefArchitect
DeepScout
DataAnalyst
CodeWizard
CriticMaster
LeadWriter
```

`RouteNode` 是 Graph 控制节点，不是 Agent；`Planner` 是 ChiefArchitect 的规划职责，不是 Agent 名称。

```text
Agent   → 语义判断、研究执行、分析、审核与写作
Graph   → 路由、并发、合流、循环与终态
Harness → Context、Agent Loop、Tool Policy、预算与恢复
Tool    → 原子执行能力
Skill   → 方法、步骤与领域工作流
```

## 1. 协作结构

```text
Query Completion
  → RouteNode
       ├─ direct  → ChiefArchitect → assistant Message
       ├─ focused → DeepScout | DataAnalyst | CodeWizard → assistant Message
       └─ deep    → ChiefArchitect / Plan
                         ↓
                  DeepScout + DataAnalyst
                         ↓
                    MergeEvidence
                         ↓
                  CodeWizard（按需）
                         ↓
                  Coverage → CriticMaster
                         ↓
                     LeadWriter
```

Agent 通过 ResearchState 的结构化字段、Evidence 引用和 Harness 生成的最小 Context Packet 交接。长内容保存在 Workspace，知识库内容保存在 Document。

## 2. 公共 Agent Contract

运行时任务与研究业务状态保持分离：

```text
ResearchTask
  → 由 Runtime 管理
  → task_id / thread_id / status / 调度 / pause / resume / cancel

ResearchState
  → 由 Graph 管理
  → query / route / plan / evidence / coverage / critic / report

Agent
  → 接收 Harness 投影的 Context Packet
  → 返回业务结果
  → 不直接读取或修改 ResearchTask 记录
```

每个 Agent：

```text
1. 只接收当前职责需要的 Context Packet
2. 只使用自身 Tool Policy 和 Skill whitelist
3. 通过 Evidence 引用读取必要的 Workspace / Document 内容
4. 返回结构化业务结果，由 Graph Node 生成并提交其拥有的 ResearchState patch
5. 不复制完整 ResearchState，不覆盖原始 Query、Evidence 或 ToolResult
6. 不修改 ResearchTask.status、claim、lease、pause、resume 或 cancel 状态
```

Harness 统一管理有界 ReAct、deadline、TaskBudget、模型 Binding、取消和恢复。`task_id`、`remaining_budget`、`deadline`、`cancellation_token` 可以作为只读运行信息注入 Context Packet，但不成为 Agent 输出，也不进入 ResearchState。

## 3. 六个 Agent

### 3.1 ChiefArchitect

**绑定节点**：`Direct` · `Plan` · `Deepen` · `Replan`

**定位**：面向用户的通用对话与研究规划角色。

```text
Direct
  → 回答闲聊、解释、通用知识与研究拆解建议
  → 不调用 Tool

Plan
  → 为 deep 任务生成 Hypothesis Plan

Deepen
  → 根据 Coverage / Critic 缺口增加 Hypothesis、Evidence Need 或 Query

Replan
  → 用户改变目标后，基于新 query 生成新 Plan
```

ChiefArchitect 使用一次结构化推理，不运行 ReAct，也不持有 Web、RAG、SQL、Document、Compute 或其他外部 Tool。

### 3.2 DeepScout

**绑定节点**：`Focused(deep_scout)` · `Web` · `RAG`

**定位**：围绕当前 Hypothesis 执行 Web、arXiv、Document RAG 和文档精读，并把完成读取、定位与判断的结果写入对应 research branch。

```text
SearchHit
  → 精确去重 / 近重复识别
  → 来源质量初筛
  → Fetch / Read
  → Passage Rerank
  → support | refute | context
  → adopted Web / RAG branch result
```

DeepScout 使用有界 ReAct。它可以查询当前 Task 已生成的 Knowledge Graph；KG 构建和更新仍由 Graph 的派生流程负责。无关候选不进入 branch，正式 Evidence 只由 MergeEvidence 创建。

### 3.3 DataAnalyst

**绑定节点**：`Focused(data_analyst)` · `SQL` · `PrepareWrite`

**定位**：负责数据层，包括数据库查询、任务级 Dataset、指标口径与结构化结果解释。

```text
外部数据库 → inspect_schema / sample_table / execute_readonly_sql
Dataset    → inspect_dataset / profile_dataset / query_dataset
短结果     → adopted SQL branch result.data
长结果     → Workspace + DatasetRef
```

DataAnalyst 使用有界 ReAct，只输出 adopted SQL branch results / DatasetRef；它不写 `evidence_pool`，不生成 AnalysisRequest，也不执行 Python 统计计算。显式数据库写请求进入既有 PrepareWrite / HITL 路径。

### 3.4 CodeWizard

**绑定节点**：`Focused(code_wizard)` · `Compute`

**定位**：根据唯一的 AnalysisRequest 执行计算层工作。

```text
AnalysisRequest + Evidence / DatasetRef
  → sandbox_execute / generate_chart / render_table
  → compute/{tool_call_id}/code.py? + manifest.json + outputs/**
  → adopted Compute branch results
  → MergeEvidence 创建 Derived Evidence
```

CodeWizard 使用有界 ReAct。Sandbox 输出始终写入 Workspace；stdout / stderr 只用于诊断。CodeWizard 只写 Compute branch 和所执行 AnalysisRequest 的状态；MergeEvidence 创建带 `derived_from + execution_ref` 的 Derived Evidence。

### 3.5 CriticMaster

**绑定节点**：`Critic`

**定位**：在写作前审核研究的语义充分性。

```text
query + hypotheses + coverage + Evidence
  → Evidence 是否真正支持或反驳 Hypothesis
  → 是否存在薄弱或冲突证据
  → 是否足以回答原问题
  → pass | deepen | retrieve
```

`retrieve.target_channel` 只允许 `web | rag | sql`。CriticMaster 不使用 ReAct、不调用 Tool，也不修改 Plan、Evidence 或 Draft。Writer 后不再调用 CriticMaster。

### 3.6 LeadWriter

**绑定节点**：`LeadWriter` · `Revise`

**定位**：基于审核通过的 Evidence 生成或修订 ReportAst。

```text
query + hypotheses + approved Evidence + Citation refs
  → ReportAst
  → Report Schema Validation
  → CitationValidation
```

LeadWriter 不负责重新研究，也不运行开放 Tool 循环。校验失败时只按确定性 issue 有界修订。

## 4. ReAct 边界

```text
RouteNode       → 一次结构化分类
ChiefArchitect  → 一次回答或结构化规划
DeepScout       → 有界 ReAct
DataAnalyst     → 有界 ReAct
CodeWizard      → 有界 ReAct
CriticMaster    → 结构化研究审核
LeadWriter      → 一次结构化报告生成或修订
```

是否使用 ReAct 只由角色是否需要根据 Tool Observation 决定下一步来判断。

## 5. Context Packet

Harness 根据节点职责投影输入：

```text
RouteNode
  → query + 可用执行域

Chief Direct
  → query

Chief Plan / Deepen
  → query + memory_context + available sources + remaining_budget

DeepScout
  → 当前 hypothesis + queries + 相关 Evidence 摘要 + remaining_budget

DataAnalyst
  → 当前 hypothesis + sql query + datasource/dataset scope + remaining_budget

CodeWizard
  → AnalysisRequest + Evidence/Dataset refs + remaining_budget

CriticMaster
  → query + hypotheses + coverage + Evidence

LeadWriter
  → query + hypotheses + 审核通过的 Evidence + Citation refs + write_intent
```

Context Packet 中的 `remaining_budget` 是 Runtime 的只读投影，不写入 ResearchState。

`task_id`、`deadline` 与 `cancellation_token` 由 Harness 随执行环境注入；Agent 只能读取，不能通过业务输出修改对应 ResearchTask。

## 6. Prompt Contract

```text
app/prompts/
├─ route_node.jinja2
├─ chief_architect_direct.jinja2
├─ chief_architect_plan.jinja2
├─ chief_architect_deepen.jinja2
├─ deep_scout.jinja2
├─ data_analyst.jinja2
├─ code_wizard.jinja2
├─ critic_master.jinja2
└─ lead_writer.jinja2
```

每份 Prompt 声明 Role、Goal、Context Packet、Tools/Skills 和 Output Contract；Prompt 不自行扩大 Tool 权限。

## 7. 验收标准

```text
□ 项目始终为六个 Agent；RouteNode 和 Planner 都不是 Agent
□ ChiefArchitect 无 Tool，只负责 direct 与 Plan / Deepen / Replan
□ DeepScout、DataAnalyst、CodeWizard 使用有界 ReAct
□ DataAnalyst 不产生 AnalysisRequest，CodeWizard 只消费 AnalysisRequest
□ CriticMaster 位于 Writer 之前，只审核研究语义充分性
□ LeadWriter 不重新研究，Writer 后不再调用 Critic
□ 每个 Agent 只接收职责所需 Context Packet
□ Agent 只返回业务结果，不修改 ResearchTask 生命周期或 Runtime 控制字段
```
