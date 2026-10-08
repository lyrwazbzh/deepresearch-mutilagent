# build_knowledge_graph

> 配套查询见 [`query_knowledge_graph.md`](query_knowledge_graph.md)。

## 1. 定位

Task Knowledge Graph 是当前 `ResearchTask` 围绕当前研究问题形成的派生关系视图。每个 Task 只有一份当前图，对外由 `task_id` 标识。

```text
ResearchState.query
  + plan.hypotheses[].{hypothesis_id, claim}
  + 当前 Task 的 Evidence
  → 问题相关 Evidence / span
  → Entity + Claim + qualifiers
  → Entity Resolution
  → Claim 绑定 evidence_id
  → tenant_id + task_id namespace
```

它帮助当前研究做实体关系、路径和冲突 Claim 分析，不替代 Document、Workspace、`evidence_pool` 或 Research LTM，也不形成新的 Evidence。

---

## 2. 调度与输入

Task KG Build 是内部派生 Job，不属于 Agent 可自主调用的业务 Tool，也不进入任何 Agent Tool allowlist。MergeEvidence 成功提交后，Graph / Runtime 只负责持久化构图调度；后台 KG Builder 消费已提交 Evidence，更新当前 Task namespace。六个 Agent 中只有 DeepScout 可以调用 `query_knowledge_graph`。

首次构建处理当前已采纳 Evidence，后续构建只处理新增 Evidence：

```text
MergeEvidence
  → evidence_pool
  → 持久化 KG build 调度
  → KG Builder
  → Neo4j

DeepScout
  → query_knowledge_graph
  → Claim + evidence_id
  → Evidence 回源
```

内部输入：

```text
evidence_ids: string[]       # 本轮新增或显式选择的当前 Task Evidence
```

Runtime / Graph 注入：

```text
tenant_id
task_id
ResearchState.query
plan.hypotheses[].{hypothesis_id, claim}
operation_key
```

构建器先用完整查询和 Hypothesis 筛选相关 Evidence 与片段，再从 Evidence 的 `doc_id + chunk_id`、内联 `data` 或 `workspace_path` 读取内容。Task owner 授权在调度前完成，构建器只访问该 Task 已有 Evidence。

非阻塞构建任务在写入前重新读取当前 `query + plan.hypotheses`；若与本次任务捕获的输入不同，本次结果作废并由最新研究问题重新调度。该校验使用 `task_knowledge_graphs.query_hash + plan_hash`，不向 ResearchState 增加图版本字段。

---

## 3. 输出

```text
status: ready | partial
entity_count: int
claim_count: int
relation_count: int
processed_evidence_count: int
skipped_evidence_ids?: string[]
added?: { entities: int, claims: int, relations: int }
conflicts_kept?: int
```

输出是派生更新摘要。图的身份就是当前 `task_id`，不再产生第二个公共句柄。

---

## 4. 数据模型

```text
Entity
  entity_id
  canonical_name
  entity_type
  aliases[]

Claim
  claim_id
  predicate
  subject_entity_id?
  object_entity_id? | value?
  qualifiers?                  # time / region / unit / definition / ...

Provenance
  evidence_id                  # 当前 Task evidence_pool 中的不可变 Evidence
  locator?
```

Relation 是 Claim 的边投影。每条进入图的 Claim 都有至少一个 `evidence_id`；读取原文时再由 Evidence 解析 Document、内联短结果或 Workspace 长内容。

Entity 与 Claim 使用 Task namespace 内的稳定身份：

```text
Entity identity
  → Entity Resolution 得出的 canonical entity 唯一标识
  → replay 与增量构建复用已有 entity_id

Claim identity
  → task_id
    + normalized subject
    + normalized predicate
    + normalized object/value
    + normalized qualifiers
  → 生成稳定 claim_key

相同 claim_key
  → 复用已有 claim_id
  → 只追加新的 Provenance

不同 claim_key
  → 创建新的 Claim
```

`evidence_id` 只属于 Provenance，不参与 Entity 或 Claim identity；同一事实由多个 Evidence 支持时仍然只有一个 Claim。

实体类型使用小型受控词表，并允许研究任务增加少量领域类型：

```text
Organization | Person | Product | Technology | Method
Dataset | Metric | Event | Location | Policy | Concept
```

---

## 5. 构建流程

```text
校验 evidence_ids 属于当前 Task
  → 根据 query + Hypotheses 选择相关 Evidence / span
  → Schema-guided Extraction
       Entity Mention
       Atomic Claim
       qualifiers
  → Entity Normalize / Candidate Match / Resolution
  → Claim Normalize
  → 绑定 evidence_id + locator
  → 重复 Claim 合并 provenance
  → 冲突 Claim 分别保留
  → Neo4j 增量 Upsert
```

实体解析先使用 normalized name、alias 和外部 ID 产生候选，再对候选做精判。高置信匹配合并为 Canonical Entity；不确定项保留为不同实体。

重复与冲突的处理不同：

```text
同一事实重复出现 → 合并 Claim，追加 provenance
值、时间或口径冲突 → 保留多个 Claim，各自绑定 Evidence
```

无法绑定有效 Evidence 的抽取项不进入 Task KG，并计入 `ungrounded_dropped` 观测。

---

## 6. 生命周期

```text
首次 MergeEvidence
  → 按当前 query 构建初始 Task KG

Deepen / 后续 MergeEvidence
  → 对新增 Evidence 做相关性筛选与增量 Upsert

Replan 改写 ResearchState.query
  → 清理当前 Task KG
  → 使用新 query、Hypotheses 与现有 Evidence 重建

Task 删除
  → 删除 tenant_id + task_id namespace
```

构图更新不阻塞 Coverage 或 Writer；尚未完成时，查询根据 PostgreSQL `task_knowledge_graphs.status` 返回明确的 `building` 状态。Neo4j 是可重建索引，权威来源始终是当前 Task Evidence。

Share 或 Export 需要知识图谱时，查询当前图并把 JSON 或图片快照写入 Workspace。快照是分享/导出产物，Neo4j namespace 仍由 `task_id` 标识。

---

## 7. 存储与幂等

```text
namespace = tenant_id + task_id
node/edge provenance = evidence_id + locator?
```

PostgreSQL 只保存异步派生 Job 的控制元数据：

```text
task_knowledge_graphs
  tenant_id
  task_id
  status                      # building | ready | partial
  query_hash
  plan_hash
  processed_evidence_count
  updated_at

PRIMARY KEY (tenant_id, task_id)
```

该记录只回答当前 Task KG 的构建状态，不保存研究事实或图内容。`evidence_pool` 仍是权威研究事实，Neo4j 保存派生图数据；KG lifecycle metadata 不进入 ResearchState。

相同 `operation_key` 重放返回已提交结果；新 Evidence 使用新的稳定副作用槽。Entity Upsert 复用已有 `entity_id`，Claim Upsert 按 `claim_key` 复用已有 `claim_id`，重复执行只追加尚未存在的 Provenance。Repository 支持按 Task 清理和从 Evidence 全量重建。

---

## 8. 可观测与错误

```text
entity_count, claim_count, relation_count
processed_evidence_count, skipped_evidence_count
added_*, conflicts_kept, ungrounded_dropped
idempotent_replay?
```

总耗时由 Tool Span 表达；`select / extract / resolve / upsert` 使用阶段子 Span。部分 Evidence 失败时返回 `partial` 与 `skipped_evidence_ids`；空或不属于当前 Task 的 Evidence 输入返回 `invalid_evidence_ids`。

---

## 9. 验收标准

```text
□ 每个 Task 由 task_id 标识唯一当前图
□ query 与 Hypotheses 约束进入图的 Evidence 和 Claim
□ build_knowledge_graph 是内部派生 Job，不进入 Agent Tool allowlist；只有 DeepScout 可调用 query_knowledge_graph
□ task_knowledge_graphs 只保存 building/ready/partial 等派生构建元数据，不进入 ResearchState
□ Entity 与 Claim 在 replay 和增量构建中复用稳定身份，evidence_id 只参与 Provenance
□ 每条 Claim 可通过 evidence_id 回到原始 Evidence
□ 重复 Claim 合并 provenance，冲突 Claim 分别保留
□ Deepen 增量处理新增 Evidence；Replan 按新问题重建
□ 构图不阻塞 Coverage / Writer，Neo4j 可由 Evidence 重建
□ Share / Export 通过 Workspace 快照交付图产物
```
