# DeepResearch-mutilagent：Evidence Pipeline

> 正式 `ResearchRound` / `Evidence` 数据结构以 [`state.md`](state.md) 为准；DAG 中的运行位置以 [`graph.md`](graph.md) 为准。本文只定义 Branch Result 的准入、来源归一化、稳定身份、合流和提交语义，不维护第二套 Evidence Schema。

## 1. 定位与唯一写权限

Evidence Pipeline 将执行分支已经采纳的研究结果转换为当前 `ResearchTask` 的正式 Evidence：

```text
ToolResult
  → DeepScout / DataAnalyst / CodeWizard 采纳与解释
  → research_rounds[depth].{web|rag|sql|compute}
  → MergeEvidence
  → evidence_pool
```

职责固定为：

```text
执行 Agent
  → 调用职责内 Tool
  → 完成读取、定位、相关性与 stance 判断
  → 只写所属 research_rounds branch

MergeEvidence
  → 确定性控制节点，不调用 LLM
  → 校验并归一化 adopted branch results
  → evidence_pool 的唯一写入者
```

`SearchHit`、RAG Candidate、SQL 原始响应和 Compute 诊断不是正式 Evidence。Coverage、Critic、Writer、Citation 与 Task KG 只消费已经写入 `evidence_pool` 的 Evidence。

## 2. Branch Result 准入合同

`research_rounds[depth].branch.items[]` 只保存已经被执行 Agent 采纳、可以交给 `MergeEvidence` 的结果。搜索候选、未读取正文、未完成定位或尚未判断 Hypothesis 关系的中间数据保留在节点执行过程、ToolResult 或 Trace 中，不写入 branch items。

每个 adopted branch result 必须提供：

```text
hypothesis_id
result_key                  # 当前 Task 内稳定的逻辑结果身份
summary
excerpt
stance                      # support | refute | context
relevance                   # 0..1
content_hash

以及与 branch 对应的完整内容位置和来源元数据
```

稳定结果身份由既有执行身份生成，不引入另一套运行 ID：

```text
result_key = sha256(operation_key | stable_item_slot)
```

`operation_key` 来自 NodeRun 的稳定副作用键；一次 ToolResult 产生多个 adopted item 时，`stable_item_slot` 优先使用来源自身的稳定标识，例如 Web 的 canonical URL + passage locator、RAG 的 `doc_id + chunk_id`、SQL query-result logical slot 或 Compute output path。只有 producer 明确保证确定性排序时，ordinal 才能作为 slot；本次返回数组的临时下标不构成稳定身份。retry 与 checkpoint replay 复用相同 `result_key`。

各 branch 的附加准入信息：

| Branch | 完整内容位置 | 来源信息 |
|---|---|---|
| Web | `data` 或 `workspace_path` | `source_uri + retrieved_at`，可选 `published_at` |
| RAG | `doc_id + chunk_id` | Document Chunk 的 `content_hash` |
| SQL | `data` 或 `workspace_path` | 查询结果的 `content_hash` |
| Compute | `workspace_path` | `derived_from + execution_ref + content_hash` |

`data`、`doc_id + chunk_id`、`workspace_path` 按 [`state.md`](state.md) 的内容位置规则互斥。短结果的 `data` 保存完整规范化结果；长结果和 Compute 文件使用 Workspace；RAG 内容继续保存在 Document System。

Compute Manifest 可以用 `input_datasets[]` 记录 Dataset 执行资源血缘，但 adopted Compute result 必须具有非空 `derived_from`，且其中只保存上游 `evidence_id`。DatasetRef 不成为 Evidence identity，也不写入 `derived_from`。

准入校验失败表示 branch producer 违反合同，`MergeEvidence` 以节点失败收敛并记录失败事件，不静默生成不完整 Evidence。

## 3. 来源归一化

Formal Evidence contract follows [`state.md`](state.md). `MergeEvidence` only defines validation, normalization, identity and merge semantics.

来源映射固定为：

```text
research_rounds.web     → source_type=web
research_rounds.rag     → source_type=rag
research_rounds.sql     → source_type=sql
research_rounds.compute → source_type=compute
```

归一化步骤：

```text
1. 验证 hypothesis_id 存在于当前 Plan
2. 验证 result_key 在当前 Task 内可稳定重放
3. 验证 summary / excerpt / stance / relevance
4. 验证 branch 对应的内容位置与来源字段
5. 对 inline data 重算规范化 content_hash；对 Workspace / Document 引用核对元数据 hash
6. 按 state.md 构造正式 Evidence
```

来源质量、搜索排名、RRF 分数和候选近重复分数只服务 branch 内筛选，不扩展正式 Evidence Schema。

## 4. Evidence Identity 与 Replay

`evidence_id` 由现有 Task、Hypothesis 和逻辑结果身份确定性生成：

```text
evidence_id = stable_id(
  task_id,
  hypothesis_id,
  branch,
  result_key
)
```

这条规则产生以下语义：

```text
同一逻辑 ToolResult item retry / replay
  → 相同 result_key
  → 相同 evidence_id
  → 不重复追加

新的语义 Tool 调用
  → 新 operation_key / result_key
  → 即使 content_hash 相同，也形成独立 Evidence
```

`evidence_pool` 已存在相同 `evidence_id` 且不可变字段一致时，Merge 是 no-op；相同 ID 对应不同来源、内容位置或 `content_hash` 时视为一致性错误。Evidence 创建后，来源、内容位置、`data` 和 `content_hash` 在当前 Task 生命周期内保持不变。

MergeEvidence 对 `evidence_pool` 提交按 `evidence_id` 计算的增量 merge patch：只追加新的 Evidence，replay 项执行 no-op，已有 Evidence 保持原值；不通过重新计算并整体覆盖列表完成合流。

## 5. 去重边界

候选去重与 Evidence 合流是两个阶段：

```text
候选阶段
  → Web 使用 arXiv ID / DOI / canonical URL 精确去重
  → 可使用 SimHash / embedding 识别近重复
  → RAG / SQL 使用各自的候选与查询结果规则
  → 目标是减少重复读取和模型计算

Evidence 阶段
  → 只按 evidence_id 识别同一逻辑结果的 replay
  → 不按 content_hash 跨来源合并
  → 不做跨来源语义合并
```

`content_hash` 只校验 Evidence 指向内容的完整性。不同检索调用、不同 URL、不同 Document Chunk 或不同 SQL 查询即使返回相同内容，也保留各自的来源追溯。

## 6. Hypothesis 归属

每张 Evidence 卡片只绑定一个 `hypothesis_id`。执行 Agent 在写入 branch item 前完成 `support | refute | context` 判断和相关性评分。

```text
同一来源只支持 Hypothesis A
  → 一张绑定 A 的 Evidence

同一来源同时支持 Hypothesis A 与 B
  → 两个 adopted branch results
  → 两张 Evidence
  → 可以共享来源引用和 content_hash
  → evidence_id 因 hypothesis_id 不同而不同
```

没有明确 Hypothesis 归属的结果不进入 branch items。Coverage 按 Hypothesis 检查结构完整性，Critic 再判断 Evidence 的语义充分性。

## 7. 分支终态与 Merge 时机

当前 round 参与执行的 Web / RAG / SQL branch 由 Plan Query 决定；未调度的 branch 使用 `skipped`。Branch 状态语义为：

```text
pending    → 尚未达到合流点
completed  → 执行完成，可以包含零个或多个 adopted items
failed     → 分支执行失败；可以保留失败前已经完整提交的 adopted items
skipped    → 当前 round 未调度该 branch
```

`completed | failed | skipped` 均为终态。当前合流范围内的 branch 全部终态后，Graph 才调度 `MergeEvidence`：

```text
Retrieval 合流
  → 读取当前 depth 的 web / rag / sql

Compute 合流
  → AnalysisRequest 执行完成后读取当前 depth 的 compute
  → 复用同一 MergeEvidence 规则
```

`failed` branch 不阻止其他 branch 或本 branch 已完整提交的 adopted items 被归一化。Branch failure 和空结果继续保留在 `research_rounds`，由 Coverage 判断缺口、Deepen 或停止条件；`MergeEvidence` 不替代 Coverage 做流程决策。

MergeEvidence 可以在同一 depth 被再次调用。确定性 `evidence_id` 与已有 `evidence_pool` 比较保证先前结果不会重复追加。

## 8. 原子提交与测试

MergeEvidence 复用现有节点执行与恢复链：

```text
Graph join
  → NodeRunner.begin(MergeEvidence)
  → handler 读取当前 round + 已有 evidence_pool
  → 构造按 evidence_id 合并的 evidence_pool 增量 patch
  → StateManager.commit_node(
       node_run_id,
       patch,
       expected_lease_token
     )
  → 同一事务提交 State + checkpoint + Event + Outbox + NodeRun
```

字段所有权：

```text
DeepScout   → research_rounds[depth].web / rag
DataAnalyst → research_rounds[depth].sql
CodeWizard  → research_rounds[depth].compute、所执行 AnalysisRequest.status
MergeEvidence → evidence_pool
```

MergeEvidence 不修改 `research_rounds`、Plan、Coverage 或 ResearchTask Runtime 字段。MergeEvidence commit 在同一 PostgreSQL 事务中写入 KG 派生更新对应的 Event / Outbox；事务提交后由异步消费者更新 Task KG。KG 本身不参与 Evidence 合流事务。

最低测试集：

```text
□ 执行 Agent patch evidence_pool → PatchOwnershipError
□ MergeEvidence 是 evidence_pool 的唯一写入节点
□ Web / RAG / SQL / Compute 分别正确映射到 state.md Evidence
□ pending branch 阻止 Merge；completed / failed / skipped 允许到达合流点
□ failed branch 不丢失此前已经完整提交的 adopted items
□ 同一 result_key replay 复用 evidence_id 且不重复追加
□ stable_item_slot 使用来源稳定标识或有确定性排序保证的 ordinal
□ MergeEvidence 只提交增量 merge patch，不整体覆盖已有 evidence_pool
□ 不同来源或不同调用具有相同 content_hash 时仍保留独立 Evidence
□ 同一来源绑定两个 Hypothesis 时生成两个 evidence_id
□ inline / Document / Workspace 内容位置互斥并校验 content_hash
□ Compute Evidence 保留 derived_from 与 execution_ref
□ Dataset 输入只进入 manifest.input_datasets，derived_from 仍是非空 Evidence ID 列表
□ Evidence 与 KG 更新意图在同一事务提交，异步消费者据此更新 Task KG
□ MergeEvidence NodeRun replay 返回已提交 checkpoint
```

### 验收标准

```text
□ state.md 是 ResearchRound / Evidence Schema 唯一事实源
□ graph.md 只定义 MergeEvidence 的 DAG 位置与路由
□ 本文是 branch admission、normalization、identity、dedup、merge 与 commit 语义唯一事实源
□ Tool 与 Agent 只产出 adopted branch results，不直接写 Evidence
□ memory.md 不维护正式 Evidence 字段清单
□ retry、recovery、失败分支和多 Hypothesis 行为均可确定性验证
```
