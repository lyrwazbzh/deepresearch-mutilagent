# DeepResearch-mutilagent：研究长期记忆与长结果外置

## 0. 四类数据职责

```text
evidence_pool      = 当前 ResearchTask 的统一证据入口；保存完整短结果、摘要和稳定引用
Document           = 可跨 Task 检索的知识库文件、切片与 RAG 索引
Workspace          = 当前 Task 的工作记忆文件系统
Research LTM       = 跨 Task 的用户研究偏好、背景、方法、目标、纠正与稳定协作上下文
```

四者的职责：

- `evidence_pool` 为 Coverage、Critic 和 Writer 提供合流后的证据卡片。
- Document 保存可跨 Task 检索的知识库资料。
- Workspace 保存当前 Task 的 Web、SQL、Compute、图表和报告等长内容，作为工作记忆。
- Research LTM 为规划提供可跨 Task 复用的用户研究协作先验。
- `conversation_messages` 保存 Thread 对话原文；任务创建 / Replan 入口以当前用户消息为截止点，读取同 Thread 有界近期消息来补全 `ResearchState.query`，完成轮次还可触发后台 Memory Extractor。

近期消息按持久化顺序选择，只包含不晚于当前用户消息的 user/assistant 消息，并使用固定消息数与 token budget。当前用户输入优先于历史内容；补全结果直接写入唯一的 `ResearchState.query`，重试复用该结果。

---

## 1. evidence_pool 与文件层

### 1.1 evidence_pool 只存证据卡片

`ResearchState.evidence_pool[]` 由 `MergeEvidence` 生成。正式 Evidence Schema 只由 [`state.md`](../01_architecture/state.md) 定义；Branch Result 的准入、归一化、identity、去重与提交语义只由 [`evidence_pipeline.md`](../01_architecture/evidence_pipeline.md) 定义。本文只说明 Evidence 与文件层、Research LTM 的职责边界。

`data` 是 Web / SQL 的完整规范化短结果，并保留 `truncated / has_more / next_cursor` 等完整性标记；这些标记表达上游分页或硬限制，不用于主动截短超出 inline budget 的完整内容。`summary` 只用于快速阅读。Dataset 通过 SQL/Data 模块与 DuckDB 查询，Evidence 仍使用 `source_type=sql`。Compute 的 stdout / stderr 只作为运行诊断，计算结果和生成文件写入 Workspace。

Evidence 随 Graph checkpoint 保存，用于恢复、Coverage、Critic 和 Writer。知识库正文保存在 Document；Web / SQL 长结果与 Compute 文件写入 Workspace。

MergeEvidence 将各分支结果规范化并汇入统一列表；具体合流规则由 [`evidence_pipeline.md`](../01_architecture/evidence_pipeline.md) 负责。`content_hash` 用于内容完整性校验，而不是跨来源去重。

Evidence 创建后，来源字段、内容位置、`data` 与 `content_hash` 保持不变。来源或内容变化时生成新的 Evidence；已被报告引用的 Evidence 在 Task 生命周期内持续保留。

### 1.2 短结果直接返回，长结果进入 Workspace

```text
Web / SQL
  → 完整短结果进入 ToolResult.data
  → 超过 inline budget 或明确需要文件消费时写入 Workspace
  → ToolResult 返回 data 或 summary + workspace_path + content_hash

Compute
  → 计算结果与生成文件全部写入 Workspace
  → stdout / stderr 不进入 Evidence

RAG
  → 返回 summary + doc_id + chunk_id + content_hash

research_rounds
  → 保存完整短结果和稳定引用
  → MergeEvidence 规范化并汇入 evidence_pool
```

需要正文时，Harness 根据 `ResearchState` 中的引用为当前职责读取 Document 知识库或 Workspace 工作记忆，并把 token budget 内的必要片段放入 Context Packet。

### 1.3 Document 与 Workspace 的归属不同

- **Document**：上传或已入库、可索引、可被多个 Task 引用的知识库对象；正文在 MinIO，元数据在 PostgreSQL，检索索引在 Elasticsearch。
- **Workspace**：当前 Task 的工作记忆文件目录；保存本次研究产生的长结果与派生文件。

Document RAG 返回 `doc_id/chunk_id`；Web / SQL 长结果与 Compute 生成文件写入 Workspace。Dataset 归入 SQL/Data 能力。详细生命周期见 [`document_system.md`](document_system.md) 与 [`research_workspace.md`](research_workspace.md)。

---

## 2. Research LTM 的保存范围

Research LTM 是 `tenant_id + user_id` 隔离、可跨 ResearchTask 使用的用户级长期记忆。P0 只有一张业务表：

```text
research_memories
```

允许保存：

```text
research_preference    # 研究偏好
research_background    # 长期研究背景
research_method        # 稳定研究方法
output_preference      # 输出语言、结构、详略等习惯
long_term_goal         # 跨 Task 长期目标
collaboration_context  # 稳定协作方式
```

分类边界：

- `research_preference`：关注范围、来源偏好、研究深度与稳定约束。
- `research_background`：用户角色、行业、项目等长期背景。
- `research_method`：验证、比较和分析等稳定方法。
- `output_preference`：语言、结构、详略与格式。
- `long_term_goal`：跨 Task 持续目标。
- `collaboration_context`：确认、交接与协作习惯。

明确纠正通过 `source=explicit_correction` 表示；纠正后的条目仍使用其 canonical topic。

---

## 3. 单表合同

```text
research_memories
  memory_id              uuid PK
  tenant_id              uuid NOT NULL
  user_id                uuid NOT NULL

  topic                  text NOT NULL
  content                text NOT NULL

  source                 text NOT NULL
  supporting_message_ids uuid[]?
  confidence             numeric(3,2) NOT NULL

  embedding              vector?
  content_hash           char(64) NOT NULL
  write_key              char(64) NOT NULL

  status                 text NOT NULL
  related_memory_id      uuid?

  revision               integer NOT NULL DEFAULT 1
  created_at             timestamptz NOT NULL
  updated_at             timestamptz NOT NULL
```

### 3.1 字段语义

`topic` 是服务端规范化后的原子记忆槽位，也是冲突检测和单活约束的主键语义，例如：

```text
output.language.default
output.detail.default
research.source.preference
research.method.validation
background.user.industry
goal.long_term.primary
collaboration.document_workspace_boundary
```

Topic Registry 是随代码发布、可版本化的配置。每个条目至少包含：

```text
topic          # canonical topic
category       # 六类业务分类之一
base_memory    # 是否作为统一召回中的全局优先项
aliases[]      # 允许映射到该 topic 的受控别名
```



`source` 表示记忆写入路径：

```text
explicit_remember      # 用户明确要求记住
turn_completion        # 用户可见对话轮次完成后后台提取
explicit_correction    # 用户明确纠正或替换既有记忆
manual_edit            # 用户通过记忆管理入口修改
```

`supporting_message_ids` 保存直接支持该记忆的用户消息。`thread_id/task_id` 通过消息记录反查；`input_hash/extractor_version/prompt_version` 保存在 MemoryExtraction Job 与 Trace；写入幂等由 `write_key` 负责。

`confidence` 表示“这是用户本人表达且跨 Task 仍有效的长期记忆”的确定度：

```text
explicit_remember / explicit_correction / manual_edit = 1.00
直接且明确的长期用户陈述                         = 0.90～0.95
最低自动写入阈值                                 = 0.70
```

`status`：

```text
active
pending_conflict
superseded
forgotten
```

`related_memory_id` 与当前行使用相同 `tenant_id + user_id`：`pending_conflict` 指向发生冲突的 active 记忆，替换产生的新 active 记忆指向被 supersede 的旧记忆。`revision` 用于 CAS 并发控制。

`embedding` 可空。正文先提交 PostgreSQL，新建或 content 改变时由同事务 outbox 异步生成向量；向量为空时召回使用 topic/lexical 路径，失败任务由异步 Job 的有限重试与恢复扫描处理。

### 3.2 约束与索引

```text
INDEX  (tenant_id, user_id, status, topic)
VECTOR INDEX (embedding) WHERE embedding IS NOT NULL

UNIQUE (tenant_id, user_id, write_key)
UNIQUE (tenant_id, user_id, topic, content_hash)
  WHERE status IN ('active', 'pending_conflict')
UNIQUE (tenant_id, user_id, topic)
  WHERE status = 'active'
```

`content_hash = SHA256(NFKC + trim + whitespace/punctuation normalization 后的 content)`。Tokenizer 随服务版本统一发布：拉丁文本按单词切分，CJK 文本按字符 bigram 切分，数字与否定词保留。召回的 `lexical_score = |Q∩M| / sqrt(|Q|*|M|)`，空集合得 0。


---

## 4. 写入链

### 4.1 自动提取

自动提取在一次用户可见轮次完成、final assistant message 成功落库后触发。

```text
final assistant message committed
→ 同事务写 MemoryExtractionRequested outbox
→ Worker 按 tenant/user/thread 防抖 30 秒并合并事件
→ 最多读取该批 20 条 message / 4000 input tokens
→ 过滤 system/tool/hidden/纯确认/临时要求/长正文
→ user message 作为记忆事实的唯一依据；final assistant message 只提供轮次语境
→ LLM 最多提取 5 个 durable-memory candidate
→ 每个 candidate 必须返回 supporting_user_message_ids
→ candidate 内容须由 supporting user messages 直接支持
→ scope/category/topic/authority/confidence/security gate
→ exact dedup / same-topic conflict handling
→ 单事务写 research_memories + MemoryEmbeddingRequested outbox
```


自动提取失败由 side-job 有限重试和失败状态处理并纳入可观测；已完成 Task 保持原状态。记忆写入事务提交后确认 outbox 消费。

### 4.2 显式记忆与手工修改

显式“记住”、用户确认和手工修改走同步路径：

```text
persist user command
→ 同步结构化提取与 gate
→ 同事务提交 research_memories + MemoryEmbeddingRequested outbox
→ 成功后才返回“已记住”与 memory_id/revision
```


### 4.3 并发与幂等

写入使用 PostgreSQL transaction；`write_key`、partial unique constraint 和 CAS `revision` 共同保证并发与幂等。并发插入由唯一约束裁决，显式替换通过带预期 `revision` 的条件更新提交。

Extractor 采用确定性配置。MemoryExtraction Job 与 Trace 保存 `input_hash + extractor/prompt version`；后台重试使用同一 `write_key` 复用既有处理结果。


---

## 5. 去重、纠正与冲突

### 5.1 exact dedup

相同 topic 与 `content_hash`：

- 复用现有行；
- `confidence = max(old,new)`；
- 合并 `supporting_message_ids`；
- 增加 `revision`。

不同 canonical topic 的候选分别保存。Topic Normalizer 与 aliases 负责把等价表达映射到同一原子 topic。


### 5.2 明确纠正或权威替换

显式 remember、explicit correction 或 manual edit 与同一 topic 的旧 active 内容不同时，单事务执行：

```text
old.status              = superseded
new.status              = active
new.related_memory_id   = old.memory_id
```


### 5.3 自动提取发现含糊冲突

后台候选与 active 记忆冲突，但用户没有明确纠正信号时：

```text
old.status              = active
new.status              = pending_conflict
new.related_memory_id   = old.memory_id
```

召回集合使用 `active` 条目；`pending_conflict` 通过记忆管理入口解决：

```text
GET  /memories?status=pending_conflict
POST /memories/{memory_id}/resolve   # accept_new | keep_old + expected revisions
```


---

## 6. 召回与注入


```text
LTM_CANDIDATE_K=20
LTM_TOP_K=8
LTM_MIN_SIMILARITY=0.55
LTM_MIN_LEXICAL_SCORE=0.20
LTM_TOKEN_BUDGET=1200
LTM_WRITE_CONFIDENCE_THRESHOLD=0.70
LTM_MAX_CONTENT_CHARS=500
```

`MemoryService.recall` 执行一次统一召回：

```text
tenant_id + user_id + status=active
  → Topic Normalizer 从 planning query 得到 query_topics[]
  → base_memory=true、exact topic、semantic 或 lexical 命中组成候选集
  → 最多保留 20 个候选
  → 统一排序与去重
  → Top-K 8，并受总计 1200 tokens 限制
```

`base_memory=true` 表示输出偏好、协作习惯等全局记忆在统一排序中优先，不再形成独立召回池。Exact topic 优先于普通相关性；其余候选按下式排序：

```text
relevance_score = 0.70 * semantic_score
                + 0.20 * lexical_score
                + 0.10 * confidence
```

候选满足以下任一条件即可进入排序：

```text
base_memory = true
OR canonical topic exact match
OR semantic_score >= 0.55
OR lexical_score >= 0.20
```

`embedding` 为空时 `semantic_score=0`，仍可通过 base、topic 或 lexical 路径召回。


查询构造：

```text
Plan    = ResearchState.query
Replan  = 覆盖后的 ResearchState.query
Deepen  = ResearchState.query + coverage_gaps + deepen_queries
```

其中 `ResearchState.query` 已在入口由“当前用户输入 + 同 Thread 近期对话”补全。原文保存在 `conversation_messages`；召回直接使用补全后的唯一 query。

每次 Deepen 都可重新召回。`memory_scope_etag` 仅作为 MemoryService 内部缓存失效标识；记忆写入、纠正、冲突解决、遗忘或 embedding 更新时刷新缓存。


```text
这些记忆用于调整研究规划、方法与呈现偏好。
```

Plan checkpoint 在 `ResearchState.memory_context` 保存 `query_hash` 及实际使用的 `memory_id + revision + content_hash`。调用模型前，MemoryService 校验引用的 revision/status，从 `research_memories` 读取内容并按总 token budget 渲染。缓存变化时重新召回，普通节点重试复用已保存的引用。

---

## 7. 遗忘与生命周期

状态生命周期：

```text
active              参与召回
pending_conflict    等待用户解决，不参与召回
superseded          180 天后硬删除该历史行
forgotten           24 小时内硬删除该行
```

独立 Audit 保存 actor/action/resource/result。Task 删除清理 Task-scoped State/Workspace；账号或租户删除按隐私生命周期清理对应记忆和向量。

`forget(memory_id)` 的隐私语义高于重放一致性：立即停止召回并使引用该 revision 的未来 snapshot 失效，随后在 24 小时内硬删除正文和 embedding。

---

## 8. Graph 使用方式

```text
Plan / Replan / Deepen
  → MemoryService.recall(tenant_id, user_id, planning_query)
  → 只把有界、结构化 LTM snapshot 作为规划先验

Retrieval branches
  → RAG 返回 Document 引用
  → Web / SQL 长结果写 Workspace
  → Web / SQL 短结果提交完整 data
  → 分支提交 summary + data 或引用

Post-Merge Analysis / Compute
  → 根据 AnalysisRequest 读取 Evidence/Dataset refs
  → Compute 文件写 Workspace，Derived Evidence 回指输入 Evidence

MergeEvidence
  → 生成 evidence_pool

CriticMaster / LeadWriter
  → 只消费 evidence_pool
  → 按引用读取 Document/Workspace

Research LTM
  → 作为 Plan / Replan / Deepen 的规划先验

final assistant message committed
  → MemoryExtractionRequested outbox
  → 后台提取用户 durable memory

explicit remember / correction / manual edit
  → 同步 MemoryService 写入
```

---

## 9. 验收

```text
□ Research LTM 使用 research_memories 单表，status 与 revision 表达冲突和版本
□ 只保存用户长期研究偏好、背景、方法、输出习惯、目标和稳定协作上下文
□ source、confidence、topic 的语义与 Evidence 元数据完全隔离
□ 自动提取以 user message 为唯一记忆事实依据；final assistant message 只提供语境
□ 每条候选都有 supporting_user_message_ids，且内容可由对应用户消息直接支持
□ research_memories 只保存 supporting_message_ids，提取版本和输入哈希进入 Job / Trace
□ explicit remember/correction/manual edit 同步提交成功后才确认
□ write_key、partial unique 与 revision CAS 保证重试和并发幂等
□ Topic Registry 使用 category/base_memory/aliases 承接原子 topic 规范化
□ exact duplicate 复用现有行；同 topic 按显式替换或自动冲突规则处理
□ 显式纠正覆盖；含糊冲突 pending 等待用户解决；过期 revision 返回 409 stale_conflict
□ base、topic、semantic 与 lexical 候选进入一次统一召回
□ LTM 只在 Plan/Replan/Deepen 召回；总 Top-K 8、1200 tokens
□ tenant/user 限定召回范围；embedding 为空时按 base/topic/lexical 降级
□ LTM 服务 Plan/Replan/Deepen，evidence_pool 服务 Coverage/Critic/Writer
□ Evidence 保留完整 Web / SQL 短结果；Dataset 归入 SQL，Compute 结果始终引用 Workspace 文件
□ MergeEvidence 只做多分支合流、schema 归一与重放复用，不按 hash 跨来源合并
□ Evidence 在 Task 生命周期内不可变并保留到引用它的报告一同清理
□ ResearchState/checkpoint 保存 query_hash 与 memory refs，memory_scope_etag 留在 MemoryService 内部
□ pending_conflict、superseded、forgotten 均有确定生命周期
□ forget/correction 使旧 snapshot 失效；Task 删除清理 Task-scoped State/Workspace
```

---

## 10. 实施顺序

1. `research_memories` DDL、nullable embedding、状态索引、pgvector partial index、partial unique、RLS/scope 与 Topic Registry。
2. `MemoryService.remember / extract_batch / recall / resolve / forget`，含 exact dedup、同 topic 替换/冲突与 CAS。
3. completed-turn outbox、30 秒 debounce Worker、确定性 Extractor、write-key 幂等与 embedding 异步补全/重试。
4. Plan/Replan/Deepen 的统一召回、base/topic/semantic/lexical 排序、checkpoint 引用与缓存失效规则。
5. 记忆管理 API：list、pending conflict CAS resolve、manual edit、forget。
6. 接入 [`evaluation.md`](../05_quality/evaluation.md) 的 LTM Write Gate 与 Cross-task Recall；冲突、隔离、预算和生命周期按本文合同进入场景测试。
