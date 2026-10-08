# DeepResearch-mutilagent：LLM 应用工程（Model Abstraction）

## 0. 定位


解决四件事：

```text
模型怎么配置
这次应该拿哪个模型
不同 Provider 怎么统一调用
失败怎么重试 / 降级
```

```text
配置阶段：
ModelConfig
  → ModelResolver
  → 固定 Model Binding
  → 保存到 ResearchTask Runtime Envelope

执行阶段：
Agent / Ingestion / RAG
  → ModelRuntime(binding)
  → Provider Client
  → Model Provider

```


| 能力 | 文档 |
|------|------|
| API Key 加密 / 解密 | [`auth_security.md`](auth_security.md)（SecretsService） |
| SSE / Event 推前端 | [`api_lifecycle.md`](api_lifecycle.md) |
| Token / Cost / Trace | [`observability.md`](../05_quality/observability.md) |
| Retry / CB / Fallback 通用策略 | [`runtime.md`](../03_runtime/runtime.md) / [`tool.md`](../02_capabilities/tool.md) Reliability |


---

## 1. ModelConfig

```text

model_config_id                       # 逻辑配置 ID
version                               # 从 1 递增；与 id 组成主键
tenant_id
user_id
provider
model
base_url
purpose                 # chat | embedding | rerank
secret_ref              # → SecretsService；库内无明文 Key
created_at

PRIMARY KEY(model_config_id, version)
UNIQUE(tenant_id, model_config_id, version)          # tenant-scoped FK
UNIQUE(tenant_id, user_id, model_config_id, version) # 供 tenant-scoped FK 使用

model_config_heads                    # 可变选择控制；每个逻辑配置一行
tenant_id, user_id
model_config_id
current_version
enabled_for_new_tasks
updated_at
PRIMARY KEY(tenant_id, model_config_id)
FOREIGN KEY(tenant_id, user_id, model_config_id, current_version) → model_configs

model_bindings                        # 一个 ModelConfig 可被多个角色复用
tenant_id
binding_key                           # routing|reasoning|tool|data|code|writing|embedding|rerank
purpose                               # chat|embedding|rerank
model_config_id
priority                              # 0 为 primary，其余为冻结 fallback 顺序
PRIMARY KEY(tenant_id, binding_key, purpose, priority)
FOREIGN KEY(tenant_id, model_config_id) → model_config_heads

model_config_revocations              # append-only 安全撤销事件
tenant_id, user_id
model_config_id, version
revoked_at, reason
PRIMARY KEY(model_config_id, version)
FOREIGN KEY(tenant_id, user_id, model_config_id, version) → model_configs
```

版本规则：

```text
POST create           → version=1
Task create           → 快照精确 {model_config_id, version} 及 fallback 版本
安全 revoke           → 为指定 revision 插入 revocation；每次调用检查，in-flight Task 使用已快照 fallback 或进入失败状态
```

```text
purpose
├─ chat
├─ embedding
└─ rerank
```

例：

```text
model = deepseek-chat
purpose = chat
```


---

## 2. ModelResolver

Agent 或 Component 使用稳定的角色 binding 请求模型。Binding 表达调用画像，不等于“每个 Agent 一份独立模型配置”：

```text
Agent / Component
  → binding_key + purpose
  → ModelResolver
  → 按 model_bindings.priority 读取 primary 与 fallback
  → 固定 primary + fallback versions
```

`tenant_id / user_id` 只用于权限、资源归属和审计，不参与模型排序。同一个 `model_config_id` 可以绑定多个 `binding_key`，因此不要求六个 Agent 分别维护六套配置。

```text
routing   → RouteNode
reasoning → ChiefArchitect + CriticMaster
tool      → DeepScout
data      → DataAnalyst
code      → CodeWizard
writing   → LeadWriter
embedding → Document ingestion
rerank    → Document retrieval
```

Task 创建时固定 `routing/reasoning/tool/data/code/writing/rerank` 实际使用的 primary 与 fallback revision；多个 binding 可以解析到同一 ModelConfig。运行、恢复、retry 与 checkpoint replay 都复用该快照，不重新调用 Resolver。Embedding 使用 Document 索引绑定的精确模型版本。

冻结结果作为当前 ResearchTask 的 `model_binding_snapshot` 持久化，属于 Runtime 配置快照，不进入 ResearchState，也不建立独立的 TaskModelBinding 业务系统：

```text
ResearchTask.model_binding_snapshot
  routing / reasoning / tool / data / code / writing / rerank
  → primary {model_config_id, version}
  → fallback[] {model_config_id, version}

ResearchState
  → 不保存 Model Binding
```

### 2.1 管理 API 与权限

```text
POST   /api/v1/model-configs
GET    /api/v1/model-configs
GET    /api/v1/model-configs/{model_config_id}/versions/{version}
POST   /api/v1/model-configs/{model_config_id}/test     # Endpoint Policy 后做最小真实请求
POST   /api/v1/model-configs/{model_config_id}/disable  # 只阻止新 Task 选择当前 head
POST   /api/v1/model-configs/{model_config_id}/versions/{version}/revoke # 安全事件；指定 revision fail-closed
PUT    /api/v1/model-bindings/{binding_key}             # 保存该角色的 primary/fallback 顺序
```


---

## 3. ModelRuntime

业务只调：

```text
build_chat_model()
embed()
rerank()
```

```text
LangGraph Agent → runtime.build_chat_model(binding)
RAG Chunk       → runtime.embed(binding, texts)
Hybrid Top-K    → runtime.rerank(binding, query, candidates)
```

ModelRuntime 使用已冻结的 Model Binding 加载精确模型版本，并统一完成 revision 撤销检查、Secret 按需解析、timeout/retry/Circuit Breaker、固定 fallback chain、错误归一化、取消/deadline 传播与 Token/Cost/Trace。ChatOpenAI、Embedding Client 与 Rerank Client 是 ModelRuntime 内部的具体 Provider Client，只处理相应 Provider 协议。ModelRuntime 是后端内部模块，不是独立服务。  

`build_chat_model()` 返回实现 LangChain ChatModel 接口的 `RuntimeManagedChatModel`。它把每次 invoke/stream 继续交给 ModelRuntime 的受控调用路径；内部可以使用 ChatOpenAI，但不会把裸 ChatOpenAI 或其他 Provider Client 交给 Agent。

---

## 4. Provider Client

```text
主策略：OpenAI-compatible → 覆盖大部分 Chat / Embedding
变化收敛到：provider + model + base_url + api_key(secret)
```

```text
ModelRuntime
├─ ChatOpenAI                 # OpenAI-compatible Chat / stream
├─ Embedding Client           # OpenAI-compatible 或对应 Provider Client
└─ Rerank Client              # 排序接口差异的薄 Client
```

现有 Client 已覆盖协议时直接复用，不再额外包装无独立职责的 Adapter；仅对不兼容协议增加薄适配。


---

## 5. 与 LangChain / LangGraph

Agent 场景复用框架能力：`streaming` / `bind_tools` / `tool calling` / `structured output`。

```text
ModelConfig → ModelResolver → 固定 Model Binding
  → ModelRuntime.build_chat_model(binding)
  → RuntimeManagedChatModel（LangChain-compatible）
  → LangGraph Agent
```

`build_chat_model` 根据 binding 构造 Runtime 管理的 ChatModel；OpenAI-compatible Chat 在内部使用 `ChatOpenAI`。所有 invoke/stream 都经过 ModelRuntime 的撤销、Secret、可靠性、fallback、deadline 与观测策略，Agent 不能绕过 Runtime 直接持有 Provider Client。

Embedding / Rerank / 非 Agent 短调用 → 使用同一 `ModelRuntime`。

```text
统一：ModelConfig · ModelResolver · ModelRuntime · Secret
场景：Agent 使用 ModelRuntime 构造的 LC 模型对象；其余调用 ModelRuntime 接口
```

---

## 6. Retry 与 Fallback

可恢复（有限 retry + backoff）：连接失败、read timeout、429、5xx。  

```text
Task 创建
  → ModelResolver 固定 primary + fallback versions

运行时 Model Call
  → primary 发生 Retryable Error
  → 当前版本有限 retry
  → 仍失败时直接使用 Task 已冻结的下一 fallback
  → 不重新调用 ModelResolver
```

Retry 分成两个职责层级：

```text
ModelRuntime
  → 负责一次模型调用内部的 timeout、有限 retry、Circuit Breaker 与冻结 fallback
  → 耗尽后返回 normalized model error

NodeRunner / Harness
  → 根据节点策略判断是否重新执行整个 Node
```

节点级 retry 不替代模型级 retry，模型级 retry 也不自行重启 Graph Node；两层均有独立上限，不能把它们无边界叠乘为隐含的 Provider 调用次数。

```text
Embedding retry/fallback
  ✓ 同一 embedding model identity + dimension 的另一个兼容 Provider endpoint

更换 embedding model 或 dimension
  → 保留现有 Chunk
  → 重新生成 embedding
  → 重建向量索引

Chunk 规则或 Parser 输出变化
  → 重新切分 Document
  → 生成新的 chunk_id
  → 重新生成 embedding
```

---

## 7. Embedding

```text
Document → Parent-Child Chunk
  → ModelRuntime.embed()
  → Elasticsearch dense_vector
```

索引配置固定记录 `embedding_model`、model revision、tokenizer identity、dimension 与 index schema。P0 baseline 与 Chunk Profile 由 [`document_system.md`](../02_capabilities/document_system.md#4-parser-与-chunk) 唯一维护。


---

## 8. Rerank

```text
BM25/IK + kNN → RRF → Candidate Top-K
  → Rerank → Final Top-K
```

Reranker 通过统一 ModelRuntime 调用；调用失败或超时时返回 RRF 排名并记录 `rerank_skipped`。  

---

## 9. Streaming ≠ SSE

```text
ModelRuntime.build_chat_model(binding)
  → ChatModel.stream() 返回 raw chunks
  → Agent Runtime 聚合本次尝试

primary stream 失败
  → 丢弃尚未展示的部分结果
  → 使用已冻结 fallback 重新生成
```

当前 SSE 不公开模型 raw token。若未来某个消费者已展示部分输出，Agent/Event 层发送 reset，前端清空旧输出后接收 fallback 结果；ModelRuntime 只返回 fallback/reset 信号，不管理前端状态。SSE 与断线恢复仍由 [`api_lifecycle.md`](api_lifecycle.md) 定义。


---

## 10. Connection Test

保存配置时发最小真实请求，校验 `API Key + Base URL + Model`：

| purpose | 探测 |
|---------|------|
| chat | 极短请求 |
| embedding | 一段短文本 |
| rerank | query + 少量 candidate |

错误粗分类：`Authentication Failed` / `Model Not Found` / `Timeout` / `Provider Error`。  

---

## 11. Security 对接

```text
用户保存 Key → SecretsService.encrypt → 密文
Resolver → 固定 model_config_id + version
ModelRuntime → 校验 revision → 按需解析 secret_ref → 构造 Provider Client
```


---

## 12. Observability 对接

ModelRuntime 为每次真实调用记录 Chat / Embedding / Rerank Trace，以及 retry、fallback、Token 与 Cost；具体字段、采样和异步落库以 [`observability.md`](../05_quality/observability.md) 为准。


---

## 13. 架构总览

```text
Agent / Component
  → binding_key + purpose
  → ModelResolver ← model_bindings.priority
  → 固定 primary + fallback Model Binding
  → ModelRuntime
       ├─ build_chat_model() → RuntimeManagedChatModel → ChatOpenAI（内部）
       ├─ embed()            → Embedding Client
       └─ rerank()           → Rerank Client
  → Model Provider

旁路：SecretsService · Runtime Reliability · Observability · Event/SSE
```

---

## 14. 实现范围

```text
✓ ModelConfig / Resolver / 固定 Model Binding
✓ frozen model_binding_snapshot 属于 ResearchTask Runtime，不进入 ResearchState
✓ ModelRuntime + RuntimeManagedChatModel / ChatOpenAI / Embedding Client / Rerank Client
✓ retry 使用 Task 已冻结 fallback，不在运行中重新解析
✓ 模型级 retry/fallback 与 NodeRunner 节点级 retry 分层
✓ Embedding 换模只重新生成向量；Chunk 规则变化才重新切分
✓ stream fallback 与 Event/SSE reset 边界清晰

```

> **ModelResolver 在 Task 创建时固定模型版本与 fallback，并保存到 ResearchTask；ModelRuntime 使用该 binding 构造受控的 LangChain-compatible 模型对象。ChatOpenAI 是 Runtime 内部的 Chat 实现，可靠性、撤销、Secret、deadline 与观测仍由 ModelRuntime 统一管理。**

---

## 15. 验收标准

```text
□ disable 只阻止新 Task 选择；revoke 使仍引用该 revision 的运行中调用失败并进入显式恢复路径
□ purpose ∈ chat|embedding|rerank；角色 binding 可复用同一 ModelConfig
□ tenant/user 只用于 scope、ownership 与 Audit，不参与模型排序
□ Task 创建后把 primary 与 fallback versions 冻结到 ResearchTask，ResearchState 不保存 Model Binding
□ 运行中、恢复与 replay 只读 frozen snapshot，不重新调用 Resolver
□ build_chat_model 返回 RuntimeManagedChatModel，不向 Agent 泄漏裸 ChatOpenAI / Provider Client
□ 可恢复错误 retry；耗尽后只使用 Task 已冻结 fallback
□ ModelRuntime 负责调用级 retry/fallback，NodeRunner 负责节点级 retry，两层不无边界叠乘
□ Embedding 换模或 dimension 变化只重新生成向量并重建向量索引
□ 仅 Chunk 规则或 Parser 输出变化时重新切分并生成新 chunk_id
□ stream fallback 保留；已展示部分输出时由 Event/SSE reset
□ Connection Test 覆盖三类 purpose
```

---

## 16. 实现落点

> 全局依赖顺序见 [`docs/README.md`](../README.md)；以下为 LLM 模块内部落点。

1. `model_configs` DDL + ModelResolver + ResearchTask frozen Model Binding。  
2. ModelRuntime + RuntimeManagedChatModel / ChatOpenAI / Embedding Client / Rerank Client。  
3. `build_chat_model()` 对接 LangGraph 专家节点。  
4. Embedding / Rerank、冻结 fallback 与 streaming reset 信号。  
5. Connection Test API；与 SecretsService / Runtime / Observability 接线。

## 附录：最小测试集

```text
□ Resolver 按 binding_key/purpose/priority 固定当前模型与 fallback
□ frozen binding 持久化于 ResearchTask，checkpoint replay 不从 ResearchState 或 Resolver 重建
□ tenant/user 变化不参与模型排序，越权 scope 无法进入候选集
□ build_chat_model 返回 RuntimeManagedChatModel，Tool calling 与 stream 通过内部 ChatOpenAI 可供 LangGraph 使用
□ Agent 直接持有裸 Provider Client 的路径不存在
□ primary retry 耗尽后使用冻结 fallback，期间不调用 Resolver
□ ModelRuntime 调用级 retry 耗尽后返回 normalized error，NodeRunner 再独立决定是否重试节点
□ Embedding 换模复用原 Chunk；Chunk 规则变化才生成新 chunk_id
□ stream 已有可见输出时发出 reset 信号并由 Event/Frontend 清空旧内容
□ 并发改版只生成单调 version；旧 Task 重放仍使用其快照版本
□ endpoint 创建与 connection test 都执行同一套 URL、DNS/IP、redirect 校验，防止 SSRF/重绑定绕过
```
