# DeepResearch-mutilagent：路由与研究规划合同

## 0. 定位

TaskService 先创建一次 ResearchTask；当前用户输入结合同 Thread 近期消息补全为唯一的 `ResearchState.query`，完成 State 初始化和调度后，再由 `RouteNode` 选择执行路径。

```text
用户请求
  → 创建 ResearchTask(status=pending)
  → 当前用户输入 + 同 Thread 近期上下文
  → Query Completion
  → 初始化 ResearchState.query
  → Scheduler claim
  → START → RouteNode
       ├─ direct  → ChiefArchitect 直接回答
       ├─ focused → 一个专业执行 Agent
       └─ deep    → ChiefArchitect 生成 Hypothesis Plan
```

`RouteNode` 是入口控制节点，不是 Agent；它不回答、不规划，也不调用业务 Tool。`Planner` 只是 ChiefArchitect 在 `Plan / Deepen / Replan` 节点承担的规划职责，不是第七个 Agent。

ResearchTask 的创建、status、claim、pause、resume 和 cancel 属于 Runtime；RouteNode、ChiefArchitect 与 Planning 合同只消费当前 `task_id + query`，不维护任务生命周期。

## 1. RouteNode

RouteNode 读取补全后的 `query` 和当前可用执行域，输出：

```yaml
route:
  mode: direct | focused | deep
  target_agent?: deep_scout | data_analyst | code_wizard
```

字段规则：

```text
direct  → target_agent 不出现；交给 ChiefArchitect，不允许业务 Tool
focused → target_agent 必填；交给一个专业执行 Agent 的有界 ReAct
deep    → target_agent 不出现；进入 Plan
```

路由同时考虑是否需要 Tool 和任务复杂度：

```text
不需要 Tool、可直接回答
→ direct

需要 Tool、一个执行域可以完成
→ focused

需要多来源、多执行域、假设验证、循环补证或正式报告
→ deep
```

同一执行域内组合多个 Tool 仍可属于 focused。P0 不增加 deterministic ToolNode。

## 2. Hypothesis Plan

只有 `route.mode=deep` 时存在 Plan：

```yaml
plan:
  hypotheses:
    - hypothesis_id: string
      claim: string
      evidence_needed: string[]
      refuted_if: string[]
      priority: int
      queries:
        - query_id: string
          channel: web | rag | sql
          text: string

  write_intent?:
    intent_id: string
    description: string
    datasource_id?: string

  deepen_history:
    - depth: int
      reason: string
      added_hypothesis_ids: string[]
      added_query_ids: string[]
```

字段职责：

```text
claim            → 待验证的独立命题
evidence_needed  → 支持或否定该命题需要的证据
refuted_if       → 能推翻命题的明确事实
priority         → 预算不足时的验证次序
queries          → Web、Document RAG 或 SQL 查询
deepen_history   → 本轮新增对象的索引，不复制 Plan
```

首轮通常生成 3～5 个 Hypothesis，每个 Hypothesis 生成 2～3 个 Query；实际数量由问题复杂度与剩余 TaskBudget 决定。Plan 只表达 Hypothesis、Retrieval Query、可选 Write Intent 与 Deepen 索引；计算需求由 Evidence 合流后的 Analysis Decision 表达。

## 3. Plan / Deepen / Replan

### 3.1 Plan

ChiefArchitect 读取：

```text
ResearchState.query
memory_context
当前 Task 可用来源的元数据
remaining_budget 只读投影
```

Document 正文不注入 Plan；RAG 分支按需读取。Plan 输出完整 Hypothesis Plan，Graph 将 `hypotheses[].queries[]` 按 `channel` 展开为 Web / RAG / SQL。

### 3.2 Deepen

Coverage 的结构缺口或 Critic 的语义缺口需要继续研究时进入 Deepen：

```text
query
plan
coverage.gaps / critic.gaps
已有 Evidence 摘要
execution.depth
刷新后的 memory_context
remaining_budget
```

ChiefArchitect 只增加具有信息增量的 Hypothesis、Evidence Need 或 Query，并在同一次 State commit 更新 `deepen_history` 与 `execution.depth`。无新增内容时写入 `coverage.stop_reason=no_new_queries`。

### 3.3 Replan

运行中用户通过 `POST /tasks/{task_id}/replan` 修改目标时，保持当前 ResearchTask，并重新完成 Query Completion 和 Route：

```text
新 instruction + 同 Thread 近期消息
  → 覆盖 ResearchState.query
  → RouteNode 重算 route
  → deep 时 ChiefArchitect 生成新 Plan
  → direct/focused 时清空当前 active plan
```

旧 Plan 与 Evidence 仍可在 checkpoint/history 中审计；当前执行只使用新 Plan 的 Hypothesis IDs。Replan 与系统驱动的 Deepen 分开：Replan 改变用户目标，Deepen 只补当前目标的研究缺口。

普通新用户请求通过 `POST /tasks` 创建新的 ResearchTask；系统驱动的 Deepen 不创建 Task。Planning 不决定采用新 Task 还是当前 Task，只消费入口已经确定的 `task_id + query`。

## 4. Post-Merge Analysis Decision

计算需求只能在取得 Evidence 后判断：

```text
Web / RAG / SQL
  → MergeEvidence
  → Post-Merge Analysis Decision
       ├─ 无需计算 → Coverage
       └─ 需要计算 → AnalysisRequest → CodeWizard
```

Post-Merge Analysis Decision 是结构化控制步骤，不是 Agent，也不调用业务 Tool。它是 `AnalysisRequest` 的唯一生产者。

输入：

```text
query
hypotheses
merged Evidence
DatasetRef
remaining_budget
```

输出：

```yaml
analysis_requests:
  - analysis_id: string
    hypothesis_id: string
    objective: string
    input_evidence_ids: string[]
    expected_output: string
```

DataAnalyst 只输出 adopted SQL branch results / DatasetRef；正式 Evidence 由 MergeEvidence 创建。ChiefArchitect、DataAnalyst 和 CriticMaster 都不直接生成 AnalysisRequest。

## 5. SQL、Dataset 与数据库写入

`channel=sql` 同时覆盖外部数据库与任务级表格文件：

```text
外部数据库
  → inspect_schema / sample_table / execute_readonly_sql

XLSX / CSV / Parquet
  → inspect_dataset / profile_dataset / query_dataset
  → DuckDB 临时只读 View
```

Dataset 属于 SQL/Data 能力，不形成独立 Graph 分支或 Evidence 类型。长结构化结果可形成 DatasetRef：

```text
workspace_path + content_hash + schema summary
```

用户明确提出数据库写入目标时，Plan 保存 `write_intent`；研究审核通过后进入既有 PrepareWrite / HITL / WriteExecutor 路径。

## 6. Coverage 与 Critic

### 6.1 CoverageGate：结构完整性

Coverage 只检查：

```text
每个 Hypothesis 是否至少存在 Evidence
计划 Query 是否到达终态
计划要求的 Web / RAG / SQL channel 是否完成
是否仍有未完成 Retrieval 分支
AnalysisRequest 是否完成或明确不需要
```

```yaml
coverage:
  complete: bool
  gaps:
    - hypothesis_id: string
      gap_type: missing_evidence | missing_channel | pending_query | pending_analysis
      channel?: web | rag | sql
      description: string
  stop_reason?: max_depth | budget_exhausted | no_new_queries
```

Coverage 不判断 Evidence 是否在语义上足以证明 Hypothesis。

### 6.2 CriticMaster：语义充分性

结构完整后，CriticMaster 判断 Evidence 是否真正支持或反驳 Hypothesis、是否存在薄弱或冲突证据，以及是否能回答原问题：

```yaml
critic:
  score: 0-10
  decision: pass | deepen | retrieve
  gaps:
    - hypothesis_id: string
      reason: string
      target_channel?: web | rag | sql
```

控制规则：

```text
pass + score >= 8 → LeadWriter
deepen             → ChiefArchitect / Deepen
retrieve           → Web / RAG / SQL
```

`decision` 是控制字段，`score` 是 rubric 诊断：`pass` 仅在 `score >= 8` 时合法；`score < 8` 时 Critic 必须在 `deepen` 与 `retrieve` 中选择与缺口类型一致的路径。`retrieve.target_channel` 取 `web | rag | sql`。

Critic 发现计算缺口时使用 `deepen`；新的 AnalysisRequest 仍只能由后续 Post-Merge Analysis Decision 产生。

## 7. 节点读写合同

| 节点 / Agent | 读取 | 产出 |
|---|---|---|
| RouteNode | query、可用执行域 | route |
| Direct / ChiefArchitect | query | assistant Message |
| Focused / 执行 Agent | query、相关资源范围 | assistant Message |
| Plan / ChiefArchitect | query、memory_context、来源元数据、remaining_budget | plan |
| Deepen / ChiefArchitect | plan、gaps、Evidence 摘要、remaining_budget | plan、execution.depth |
| Web / DeepScout | 当前 Hypothesis 与 web queries | research_rounds[depth].web |
| RAG / DeepScout | 当前 Hypothesis 与 rag queries | research_rounds[depth].rag |
| SQL / DataAnalyst | 当前 Hypothesis 与 sql queries | research_rounds[depth].sql、DatasetRef? |
| MergeEvidence | 当前轮结果、已有 evidence_pool | evidence_pool |
| Post-Merge Analysis Decision | hypotheses、Evidence、DatasetRef、remaining_budget | analysis_requests |
| Compute / CodeWizard | AnalysisRequest、Evidence/Dataset refs | research_rounds[depth].compute |
| Coverage | plan、分支状态、evidence_pool、analysis_requests | coverage |
| CriticMaster | query、hypotheses、coverage、evidence_pool | critic |
| LeadWriter | query、hypotheses、已审核 Evidence、write_intent | report.draft |
| Finalize | 校验通过的 report.draft | report.final |

表中的 `remaining_budget` 是 Runtime/Harness 的只读投影；`deadline` 与 cancellation state 同样由 Harness 注入。Planning 节点不把这些运行字段写入 ResearchState，也不修改 ResearchTask.status。

## 8. 验收标准

```text
□ RouteNode 是控制节点，不是 Agent，不调用业务 Tool
□ ChiefArchitect 只负责 direct 回答与 Plan / Deepen / Replan，不持有 Tool
□ direct / focused 不创建 Hypothesis Plan
□ deep Plan 只包含 hypotheses、queries 与可选 write_intent
□ AnalysisRequest 只有 Post-Merge Analysis Decision 一个生产者
□ DataAnalyst 不生成 AnalysisRequest
□ query.channel 与 Critic target_channel 只取 web | rag | sql
□ Coverage 只检查结构完整性，Critic 只检查语义充分性
□ Critic 位于 Writer 之前，Writer 后只运行确定性报告校验
□ ResearchTask 生命周期位于 Planning/Graph 之外，Planning 只消费 task_id + query
□ 运行中 Replan 保持当前 Task；普通新请求创建新 Task；Deepen 不创建 Task
```
