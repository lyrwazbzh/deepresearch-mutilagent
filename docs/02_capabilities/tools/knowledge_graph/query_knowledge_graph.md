# query_knowledge_graph

> 构图合同见 [`build_knowledge_graph.md`](build_knowledge_graph.md)。

## 1. 定位

查询当前 `ResearchTask` 的 Claim-centric Task KG，支持实体邻居、有限路径和实体/Claim 搜索。查询结果是已有 Evidence 的派生视图，不进入 MergeEvidence。

```text
query_knowledge_graph
  → 返回结构化 Entity / Claim / Relation / Path + evidence_id provenance

如需图形化展示
  → 由后续 Compute / Export 链路消费结构化查询结果
  → query_knowledge_graph 本身不生成 Workspace 文件
```

---

## 2. 输入

Agent Tool / API 读取入口共享的业务参数：

```text
op: neighbors | path | search
entity?: string                 # neighbors/path 起点：名称或 entity_id
target_entity?: string          # path 终点
query?: string                  # search 查询
relation?: string               # predicate 过滤
max_hops?: int = 3              # path only；hard limit = 5
limit?: int = 20                # hard limit = 100
```

参数组合：

```text
neighbors → entity
path      → entity + target_entity
search    → query
```

`tenant_id` 与 `task_id` 由 Runtime 或 API 路由注入。服务端先授权父级 ResearchTask，再查询其 `tenant_id + task_id` namespace。

Agent 侧仅 DeepScout 可以调用 `query_knowledge_graph`。ChiefArchitect、DataAnalyst、CodeWizard、CriticMaster 与 LeadWriter 均不获得该 Tool。API 查询属于资源读取入口，与 Agent Tool 调用共享同一 Query Service，但不经过 Agent Tool allowlist。

---

## 3. 输出

```text
graph_status: building | ready | partial
resolution_status?: hit | miss | ambiguous

entities: [{
  entity_id, canonical_name, entity_type, aliases?
}]

claims: [{
  claim_id
  predicate
  subject_entity_id?
  object_entity_id? | value?
  qualifiers?
  provenance: [{ evidence_id, locator? }]
}]

relations: [{
  from_entity_id
  to_entity_id?
  predicate
  claim_id
}]

paths?: [{
  entity_ids: string[]
  claim_ids: string[]
  hops: int
}]

evidence_ids: string[]
truncated?: bool
```

每个 Claim 的 provenance 指向当前 Task `evidence_pool`；调用方按 Evidence 中的 Document、内联短结果或 Workspace 引用读取原文。

---

## 4. 查询流程

```text
授权 ResearchTask
  → 加载 tenant_id + task_id namespace
  → 校验 op 对应参数与 limit / max_hops
  → EntityResolver 解析 entity / target_entity
  → relation / hop / limit filter
  → Bounded Graph Query
  → hydrate Entity + Claim + Relation / Path
  → 汇总 evidence_ids
```

实体名称唯一高置信命中时继续查询；多个候选时返回：

```text
graph_status: ready | partial
resolution_status: ambiguous
candidates: [{ entity_id, canonical_name, entity_type, score? }]
```

无匹配时返回 `resolution_status=miss` 和空 `entities / claims / relations / evidence_ids`。当前图正在首次构建或 Replan 重建时返回 `graph_status=building`，调用方稍后刷新。

---

## 5. 边界与错误

| 情况 | 结果 |
|---|---|
| 父级 Task 不可访问或不存在 | `404 not_found` |
| op 缺少对应参数 | `invalid_argument` |
| `max_hops` 或 `limit` 超范围 | `invalid_argument` |
| 图正在构建或重建 | `graph_status=building` |
| 查询超时 | `tool_error`，只读策略可重试 |

Task KG 查询只读。`search_chunks` 负责 Document 文本检索；MemoryService 负责跨 Task Research LTM 召回。

---

## 6. 可观测

```text
op, hit_count, claim_count, path_count?
resolution_status: hit | miss | ambiguous
max_hops_used?, truncated?, graph_status
```

---

## 7. 验收标准

```text
□ API path 的 task_id 是唯一公共图标识
□ 查询前授权父级 Task，并限定 tenant_id + task_id namespace
□ Agent 侧只有 DeepScout 可调用 query_knowledge_graph；API 读取入口不经过 Agent Tool allowlist
□ neighbors/path/search 参数组合和硬上限稳定
□ 命中结果返回 Claim、Relation 与 evidence_id provenance
□ 冲突 Claim 可并存返回
□ 未命中返回空结果，构建中返回 building
□ 不可访问或不属于当前授权范围的 Task 返回 404 not_found
□ 当前 Task 图内没有匹配 Entity / Claim 时返回空结果
```
