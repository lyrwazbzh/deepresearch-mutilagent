# DeepResearch-mutilagent：全链路可观测（Tracing · Cost · Audit）

## 0. 定位

Observability 回答三类问题：

```text
执行诊断 → 哪次 Worker 激活、Node attempt、模型或 Tool 调用失败或变慢
成本核算 → Token 与费用如何按 tenant、task、agent、provider、model 聚合
安全审计 → 谁对哪个敏感资源执行了什么操作，结果是什么
```

不同记录各有唯一职责：

```text
Event         = 给用户和 UI 的任务进度
Trace         = 给开发者的单次实际执行路径
Metric        = 给监控和告警的低基数聚合
Audit         = 给安全与合规的敏感操作记录
DecisionTrace = Data Flywheel 的 Route / Tool Decision 监督记录
Evaluation    = 系统版本的离线质量
```

领域状态以 ResearchTask、ResearchState、Evidence、PendingWrite、Document 等业务对象为准；Trace、Metric 与 Audit 不成为第二份业务状态。

---

## 1. OpenTelemetry 边界

```text
业务代码
  → OpenTelemetry SDK
  → BatchSpanProcessor
  → PG Exporter
  → observability_spans
```

OpenTelemetry 负责 Trace Context、Span 生命周期、Parent/Child、Span Kind 与标准协议埋点。项目只补充 DeepResearch 的 Node、Agent、Tool、Memory、Citation 与 Cost 语义。

P0 正常路径不主动采样或主动丢弃有界 Research Trace。Exporter 是 best-effort 旁路，写入失败不影响 ResearchTask 的业务提交。

---

## 2. Trace 执行模型

### 2.1 ResearchTask 与 research.run

ResearchTask 是可暂停、恢复和跨 Worker 接管的持久业务任务；`research.run` 是一次 Worker 激活：

```text
Worker claim ResearchTask
  → start research.run
  → 执行若干 Node attempt
  → 运行期间持续提交 checkpoint
  → Worker yield / paused / terminal / lease lost
  → end research.run
```

Resume、重新投递或新 Worker 接管时创建新的 `research.run`。同一个 `task_id` 可以关联多个 Trace，不增加独立 `run_id`，也不在 ResearchState 或 Checkpoint 中保存 Task 级 TraceContext。

### 2.2 Span 层级

```text
research.run                         # 一次 Worker 激活
│
├─ graph.node                        # 一次 Node handler attempt
│   ├─ gen_ai.chat?
│   ├─ tool.execute?
│   ├─ memory.operation?
│   └─ citation.validate?
│       └─ HTTP / DB / ES / Object Store CLIENT Span
│
└─ graph.node
    └─ ...
```

各层只表达一种事实：

| 层级 | 语义 |
|---|---|
| `research.run` | Worker 对 ResearchTask 的一次连续执行 |
| `graph.node` | 一次 Node handler attempt |
| `gen_ai.* / tool.execute / memory.operation / citation.validate` | 一次逻辑操作 |
| 标准 CLIENT Span | 一次实际 HTTP、数据库、Elasticsearch、对象存储或 Provider 调用 |

Agent 使用 `research.agent` 属性表达，不创建独立 Agent Span。Web、RAG、SQL 与 Compute 都通过对应 `tool.execute` 表达：

```text
Web     → tool.execute(tool.name=search_web | read_webpage)
RAG     → tool.execute(tool.name=search_documents | search_chunks | read_document)
SQL     → tool.execute(tool.name=inspect_schema | execute_readonly_sql | query_dataset)
Compute → tool.execute(tool.name=sandbox_execute | generate_chart | render_table)
```

MergeEvidence、Coverage、PrepareWrite、Finalize 等没有子调用时，只保留 `graph.node`。

### 2.3 Node attempt

`node_run_id` 表示一次语义节点执行，在 retry 与 checkpoint replay 中保持稳定。每次真实进入 handler 都创建新的 `graph.node` Span：

```text
同一 NodeRun
  → graph.node attempt 1：research.node_attempt_no=1
  → graph.node attempt 2：research.node_attempt_no=2
```

Tool 内部 retry 使用 Tool Event，不复用 `research.node_attempt_no`。

---

## 3. Span 持久化模型

```text
observability_spans
  tenant_id
  user_id?
  task_id?

  trace_id
  span_id
  parent_span_id?

  name
  kind
  started_at
  ended_at
  status_code

  attributes JSONB
  events JSONB
```

`tenant_id` 是隔离边界；`task_id` 用于 ResearchTask 查询；`user_id` 用于没有 Task 的用户级 Research LTM 操作。`kind` 保存 INTERNAL、CLIENT、PRODUCER 或 CONSUMER。OTel Resource Attribute（例如 `service.name`）随 Exporter 写入有界 JSONB，不另建业务列。

```text
PRIMARY KEY(trace_id, span_id)

INDEX(tenant_id, task_id, started_at DESC)
  WHERE task_id IS NOT NULL

INDEX(tenant_id, trace_id)
INDEX(started_at)
```

`parent_span_id` 不设外键，使异步写入、进程崩溃与 retention 清理不会阻塞 Span 落库。

查询方式：

```text
Task 运行历史
  → tenant_id + task_id
  → 按 started_at 列出 research.run

单次运行树
  → tenant_id + trace_id
  → 根据 parent_span_id 重建
```

---

## 4. Span Attributes 与 Events

### 4.1 GenAI 标准属性

ModelRuntime 对每次真实 Chat、Embedding 或 Rerank 调用记录：

```text
gen_ai.operation.name
gen_ai.provider.name
gen_ai.request.model
gen_ai.response.model?
gen_ai.usage.input_tokens
gen_ai.usage.output_tokens
```

部署配置固定所采用的 OpenTelemetry GenAI Semantic Convention 版本。模型输入、输出、System Prompt 与 Tool arguments 默认不写入 Span。

### 4.2 DeepResearch 属性

```text
research.graph_node?
research.node_run_id?
research.node_attempt_no?       # 仅 graph.node
research.agent?
research.model_attempt_no?      # 仅 gen_ai.*；一次逻辑模型调用内的真实尝试序号
research.model_attempt_kind?    # 仅 gen_ai.*；primary | retry | fallback
research.route_mode?            # direct | focused | deep；仅 RouteNode
research.depth?
research.revision_round?
research.hypothesis_count?      # 仅 Plan / Deepen 的有界计数
research.critic_decision?       # pass | deepen | retrieve
research.analysis_required?     # 仅 Post-Merge Analysis Decision
research.react_step?            # 仅专家有界 ReAct 的当前步序号

tool.name?

skill.name?                     # 仅 skill.activate 操作

retrieval.query_hash?
retrieval.result_count?
retrieval.cache_hit?

memory.operation?
memory.result?
memory.recall_routes[]?

app.cost.amount?
app.cost.currency?

error.category?                 # provider | infrastructure | model_semantic
```

`research.depth` 只用于 Retrieval / Deepen；`research.revision_round` 只用于 Writer / Revise。route、假设数量、Critic 决策、是否需要 Compute 和 ReAct 步数只记录有界控制信息，不记录假设、反馈或 Thought 正文。`research.node_run_id` 与 `research.agent` 可以投影到直接子操作 Span，支持按 NodeRun 与 Agent 聚合模型、Tool 和成本。

`error.category` 只表达稳定的错误层级：`provider` 表示 429、Provider 5xx 或 Provider timeout；`infrastructure` 表示 PostgreSQL、Redis、MinIO、Elasticsearch、网络等系统依赖失败；`model_semantic` 表示 Provider 调用成功，但结构化输出、业务语义或模型结果不满足合同。具体原因继续由现有 `error.type` 表达，不扩展高基数分类。

`tenant_id / user_id / task_id` 已物化为显式列，不在 `attributes` 中重复保存。Query 正文、Tool 输入输出、URL、Workspace 路径、Document ID 和 Evidence ID 不作为常规 Span 属性。

### 4.3 有界 Events

本节事件均为 OpenTelemetry **Span Event**，不写入面向 UI/SSE 的 `research_events`。

Tool retry 与 fallback：

```text
tool.retry
  attempt_no
  provider
  error.type

tool.fallback
  from_provider
  to_provider
  reason_code
```

领域降级：

```text
retrieval.degraded
  reason_code

memory.degraded
  reason_code
```

Citation 验证失败：

```text
citation.validation_failed
  validation.code
  claim_id?
  citation_id?
  evidence_id?
```

`claim_id / citation_id / evidence_id` 只进入失败 Event。Circuit Breaker 只在拒绝调用或状态切换时写 `circuit_breaker.rejected`、`circuit_breaker.state_changed` Event。

Event 名称、数量和属性大小均设置固定上限；`reason_code` 与 `validation.code` 使用有限枚举。

### 4.4 Scope 与 TraceContext

W3C TraceContext 传播 Trace 与 Span 身份；`tenant_id / user_id / task_id` 来自已授权的 ExecutionContext 或持久化 Job 记录，并由 Span Processor 投影到显式列。

API 请求与 Outbox 发布可以拥有各自的基础设施 Trace。Research Worker claim 后创建新的 `research.run`，使用 `task_id` 与业务任务关联；ResearchState、Checkpoint 和队列消息不承担跨暂停周期保存 TraceContext 的职责。

---

## 5. LLM 成本

每次真实模型调用单独计费，包括失败前已经产生用量的 primary、retry 与 fallback 调用：

```text
gen_ai.* Span
  → Provider + 实际 model + usage
  → PricingService
  → app.cost.amount + app.cost.currency
```

`model_pricing` Schema 由 [`storage.md`](../04_platform/storage.md) 定义。PricingService 按 `provider + model + currency + effective_at` 选择价格：使用调用开始时间选择 `effective_at` 不晚于该时间的最新记录；Task 内目标 currency 优先使用现有 `TaskBudget.currency`。Span 保存已经计算的 amount 与 currency，不增加 pricing version，也不执行汇率转换。

成本按以下维度聚合：

```text
tenant
task
agent
provider
model
operation
currency
time range
```

成本聚合必须按 currency 分组，不同 currency 不直接相加。

---

## 6. Retrieval、Evidence 与 Citation 归因

内容来源和执行过程是两条平行链：

```text
内容来源
Report Claim
  → Citation
  → Evidence
  → inline data / Document Chunk / Workspace File
```

```text
执行过程
research.run
  → graph.node
  → tool.execute / gen_ai.*
  → CLIENT Span
```

两条链通过 `tenant_id + task_id` 归入同一个 ResearchTask，不增加 Evidence 到 Span 的直接引用。

| 问题 | 权威入口 |
|---|---|
| Tool timeout、fallback、零结果、分支失败 | Trace |
| Citation 缺失、Evidence 不存在、hash 不匹配 | CitationValidator + 失败 Event |
| 正确 Document 是否应该被召回 | RAG Evaluation |
| Claim 是否真正得到 Evidence 支持 | Faithfulness Evaluation |

Evidence 的不可变内容位置继续由 [`state.md`](../01_architecture/state.md) 与 [`report_citation.md`](../02_capabilities/report_citation.md) 定义；Trace 不复制 Evidence 正文或来源引用。

---

## 7. Metrics

Metric Label 只使用有限枚举，不使用 tenant、user、task、query、URL、配置 ID 或资源 ID。

### 7.1 Research LTM

```text
memory_operation_duration_seconds{operation,status}
memory_recall_degraded_total{reason_code}
memory_embedding_pending_oldest_seconds
memory_pending_conflicts
memory_stale_conflict_total
```

```text
operation = extract | remember | embed | recall | resolve | forget
status    = succeeded | failed
```

Histogram 的 `_count` 同时表达各操作次数。候选数量、召回 routes 和 embedding 配置只进入对应 Span，不作为 Metric Label。

### 7.2 Circuit Breaker

```text
circuit_breaker_transition_total{provider,operation,from,to}
circuit_breaker_rejected_total{provider,operation}
```

`provider` 使用受控 Provider 类型，`operation` 使用受控逻辑操作名。

### 7.3 Telemetry Pipeline

```text
telemetry_export_failed_total
telemetry_dropped_spans_total
```

### 7.4 Semantic Cache

```text
semantic_cache_lookup_total{operation,result}
```

`operation` 使用受控 Tool 操作枚举，`result=hit|miss|error`。缓存 query、embedding、payload 与 tenant 不进入 Metric Label。

Task、Node retry 和预算指标由 [`runtime.md`](../03_runtime/runtime.md) 定义；本节不重复建立第二套运行指标。

---

## 8. 异步落库与 Retention

```text
Span End
  → OTel BatchSpanProcessor 的进程内有界队列
  → PG Exporter 批量写 observability_spans
```

Batch size、flush interval 与队列容量属于部署配置。队列满或 PostgreSQL 暂时不可用时累计 Telemetry Pipeline Metric，ResearchTask 继续使用业务存储完成提交。

正常路径不主动采样、不主动丢弃子 Span。Exporter 或 Batch queue 过载导致的 Span 丢失属于 Telemetry 降级，通过 `telemetry_dropped_spans_total` 暴露，不影响业务执行。后续接入 OTel Collector 时再按完整 Trace 引入统一采样。

```text
TRACE_RETENTION_DAYS=30
AUDIT_RETENTION_DAYS=365
```

Retention Job 按 `started_at / created_at` 清理；Trace 与 Audit 的保留策略相互独立。

---

## 9. Audit Trail

```text
Trace = 一次系统执行如何发生
Audit = 哪个 Actor 对哪个敏感资源执行了什么操作
```

Audit 覆盖：

- PendingWrite approve、reject 与 execute；
- Document 上传、删除；
- DataSource、ModelConfig、Tool Provider 与安全策略修改；
- Tool Policy、SSRF 与权限校验拒绝。

```text
audit_logs
  audit_id
  tenant_id
  actor_ref
  action
  resource_ref
  result
  reason_code?
  trace_id?
  created_at
```

```text
result = succeeded | denied | failed
```

示例：

```text
actor_ref    = user:{user_id}
action       = pending_write.approve
resource_ref = pending_write:{write_id}
result       = succeeded
```

`actor_ref` 支持 `user:{id}`、`system:{component}` 与 `policy:{component}`。领域对象保存用户提交的审批意见或拒绝理由；Audit 只保存稳定 `reason_code`。Audit append-only、完整写入，不经过 Trace 采样。

```text
PRIMARY KEY(audit_id)
INDEX(tenant_id, created_at DESC)
INDEX(tenant_id, resource_ref, created_at DESC)
```

---

## 10. Event、Trace、Metric、Audit 与 DecisionTrace

| 场景 | 记录 |
|---|---|
| Writer 开始 | Event 更新 UI；`graph.node` 记录执行 |
| 模型调用 | `gen_ai.*` Span 记录 Token、Cost 与状态 |
| Tool retry / fallback | `tool.execute` + Span Event + CLIENT Span |
| Citation 校验失败 | `citation.validate` + Span Event |
| 用户批准写库 | Task Event + Audit；执行另有 Trace |
| 记忆召回降级 | Memory Span Event + Metric |
| Route / Tool Decision 训练样本 | DecisionTrace，经 Data Flywheel 脱敏和标注 |
| 版本质量 | Evaluation Dataset、EvalRun 与 Case Result |

DecisionTrace 合同见 [`data_flywheel.md`](data_flywheel.md)，Evaluation 合同见 [`evaluation.md`](evaluation.md)。Observability Span 与训练数据可以同源产生，但持久化、脱敏、生命周期和用途彼此独立。

---

## 11. 隐私与容量

Span 默认保存：

```text
operation / node / agent
model / provider / token / latency / cost
tool / retry / fallback / result_count
status / error.category / error.type / bounded reason_code
```

Query、Prompt、模型输出、Tool 参数、ToolResult、Document 正文、Workspace 内容与 Credential 保留在各自业务系统，不进入常规 Span。失败 Event 只保存定位问题所需的短 ID 和稳定错误码。

JSONB attributes、events、Event count 和字符串长度均使用固定上限。Trace 查询先校验 tenant 与 Task 或用户 scope。

---

## 12. 架构总览

```text
ResearchTask
  │
  ├─ research.run #1 ─ graph.node ─ logical operation ─ CLIENT Span
  ├─ research.run #2 ─ graph.node ─ logical operation ─ CLIENT Span
  └─ ...
          │
          ↓
  OpenTelemetry SDK
          ↓
  BatchSpanProcessor
          ↓
  PG Exporter → observability_spans

独立旁路：
  Metrics       → 聚合与告警
  audit_logs    → 敏感操作审计
  DecisionTrace → Data Flywheel
  EvalRun       → 离线质量
```

---

## 13. P0 实现范围

```text
P0
├─ research.run / graph.node / gen_ai.* / tool.execute
├─ Memory、Citation 与标准 CLIENT Span
├─ 有界 attributes 与 events
├─ BatchSpanProcessor + PG Exporter
├─ Token / Cost 聚合
├─ Research LTM / Circuit Breaker / Semantic Cache / Telemetry Metrics
└─ append-only Audit
```

实现顺序：

1. OTel 初始化、ExecutionContext 投影与固定 Span/Event 命名。
2. `observability_spans` DDL、BatchSpanProcessor 与 PG Exporter。
3. ModelRuntime、ToolRuntime、MemoryService 与 CitationValidator 埋点。
4. PricingService 与成本聚合查询。
5. Research LTM、Circuit Breaker、Semantic Cache 与 Telemetry Pipeline Metrics。
6. Audit 接入 PendingWrite、Document、配置变更与安全拒绝。

---

## 14. 验收标准

```text
□ 一个 ResearchTask 可显示多次 research.run，并按时间组成完整运行历史
□ 每次 graph.node Span 对应一个 handler attempt；node_run_id 可聚合同一语义执行
□ Web、RAG、SQL 与 Compute 统一经 tool.execute 表达
□ Model primary/retry/fallback 可由 gen_ai.* 的 attempt 属性辨识；Tool retry/fallback 可由 Span Event 与 CLIENT Span 还原
□ provider / infrastructure / model_semantic 可由 error.category 稳定区分，具体原因仍由 error.type 表达
□ route、假设数量、Critic 决策、Analysis Decision 与 ReAct 步数只以有界属性记录
□ token/cost 可按 tenant/task/agent/provider/model/currency 聚合，不同 currency 不直接相加
□ Citation/Evidence 来源链与 Trace 执行链保持独立
□ Memory Metrics 无资源 ID 或配置 ID 高基数标签
□ Audit 包含 actor、action、resource、result，且不经过 Trace 采样
□ Exporter 故障不影响 ResearchTask 提交，并产生 pipeline metric
□ Span 中不包含 Secret、Prompt、Tool 大正文或 Workspace/Document 内容
□ DecisionTrace、Evaluation、Event、Trace、Metric 与 Audit 的用途不重叠
```

---

## Appendix：最小测试集

```text
□ Pause/Resume 后同一 task_id 产生多个 research.run，并可分别重建 Span Tree
□ Node retry 复用 node_run_id，同时递增 research.node_attempt_no
□ 模型 primary/retry/fallback 分别记录稳定的 model_attempt_no 与 model_attempt_kind
□ Provider 限流、基础设施故障与模型语义失败分别落入对应 error.category
□ Tool retry/fallback 生成有界 Span Event，真实外部调用生成 CLIENT Span
□ gen_ai.* 的 Token/Cost 与已知 Provider 用量样例一致
□ Task 外的用户级 Memory Span 仍可按 tenant/user scope 隔离
□ Citation validation 只在失败 Span Event 中附带 claim/citation/evidence ID
□ Batch queue 满和 PG Exporter 失败只增加 telemetry metric
□ Audit action 不编码结果，用户审批正文不复制到 audit_logs
□ Retention 清理不会因 parent_span_id 缺失而失败
```
