# 深度研究全链路可观测性设计

## 1. 目标

为深度研究建立可查询、可关联、可告警、可回放的可观测性体系，使开发者能够从一次用户 Query 出发，追踪到大纲规划、用户审批、网络或本地检索、事实提取、数据分析、代码执行、分章节写作、审核、补充研究、最终持久化和前端交付。

本设计必须同时回答四类问题：

1. **日志排错**：哪个组件在什么上下文中报错，异常栈和关联请求是什么。
2. **运行监控**：服务是否健康，吞吐、延迟、失败率、Token 和队列是否异常。
3. **AI 链路解释**：用了哪个模型、调用了哪些工具和检索器、每一步输入输出及成本是什么。
4. **业务过程审计**：研究经历了哪些阶段、何时保存检查点、为何补研或恢复，且历史事件不可被最新快照覆盖。

## 2. 已选方案与备选方案

### 2.1 已选：四层可观测性体系

- OpenTelemetry 上下文与 Langfuse 负责 AI Trace。
- JSON Log、Elastic Agent/Filebeat、可选 Logstash、Elasticsearch、Kibana 负责日志。
- Prometheus、Grafana、Alertmanager 负责指标与告警。
- PostgreSQL `research_runs` 和 `research_events` 负责业务事件流水与回放。

选择原因：项目已经运行 Elasticsearch 和 Langfuse，保留现有投入；同时用 OpenTelemetry 避免各子系统分别维护关联 ID。

### 2.2 未选：Grafana Loki + Tempo 全栈

优点是日志、Trace、指标都能在 Grafana 中关联，运维界面更统一。缺点是需要新增 Loki、Tempo，并放弃或弱化现有 Elasticsearch 和 Langfuse 投入。当前不采用。

### 2.3 未选：只接 Langfuse

实现成本最低，可以看到 LLM 调用，但不能覆盖普通应用日志、PostgreSQL/Redis/Milvus 健康、HTTP 错误率和业务事件回放。无法满足完整排错目标。

## 3. 范围

### 3.1 本次包含

- FastAPI 请求关联上下文与响应头。
- 统一 JSON 结构化日志。
- 深度研究阶段、Agent、LLM、检索、工具和持久化 Trace。
- Prometheus `/metrics` 与业务指标。
- PostgreSQL 研究运行和事件流水。
- 只读的研究运行与事件查询 API。
- 前端“过程报告”中的研究时间线与诊断摘要。
- Kibana、Prometheus、Grafana、Alertmanager 和日志采集配置。
- 默认仪表盘和最小可行动告警。
- 数据脱敏、采样、降级与保留策略。
- 单元、集成和端到端测试。

### 3.2 本次不包含

- 将后台研究迁移到 Celery、RQ 或 Kafka。
- 替换现有手写 `_run_simplified` 状态机。
- 建设跨机房高可用监控集群。
- 将所有历史日志迁移进 Elasticsearch。
- 自动把完整用户文档、网页全文或密钥写入日志。
- 为运维平台实现独立 RBAC；运行详情 API 复用现有登录和会话所有权校验。

## 4. 关联标识模型

所有关联标识必须通过 `contextvars` 和 OpenTelemetry Context 传播，不允许业务函数各自生成不相关 ID。

| 标识 | 生命周期 | 用途 |
|---|---|---|
| `request_id` | 一次 HTTP 请求 | API 请求与访问日志 |
| `session_id` | 一个聊天会话 | 会话分组与权限校验 |
| `research_id` | 一份报告从规划到完成 | 跨审批和恢复串联业务生命周期 |
| `run_id` | 一次实际执行 | 区分初始规划、批准后继续、断线恢复 |
| `trace_id` | 一次 OpenTelemetry Trace | 日志、Langfuse 与 Trace 关联 |
| `span_id` | 单个阶段或调用 | 定位 Agent、LLM、检索和工具步骤 |

规则：

- 新研究生成 `research_id`，写入检查点状态，批准和恢复沿用。
- 每次 `/research/stream`、`/outline/{session_id}/approve`、`/resume/{session_id}` 生成新的 `run_id` 和 `trace_id`。
- 客户端传入合法 `X-Request-ID` 时沿用，否则服务端生成 UUID。
- 响应返回 `X-Request-ID`、`X-Trace-ID` 和 `X-Run-ID`。
- `trace_id`、`run_id`、`session_id` 不作为 Prometheus Label。

## 5. JSON Log

### 5.1 标准字段

每条应用日志输出一行 UTF-8 JSON：

```json
{
  "@timestamp": "2026-07-19T12:30:15.123Z",
  "level": "INFO",
  "logger": "Agent.CriticMaster",
  "service": "industry-assistant-api",
  "environment": "development",
  "event": "llm_call_completed",
  "message": "LLM call completed",
  "request_id": "...",
  "session_id": "...",
  "research_id": "...",
  "run_id": "...",
  "trace_id": "...",
  "span_id": "...",
  "phase": "reviewing",
  "agent": "CriticMaster",
  "model": "qwen-plus",
  "duration_ms": 4280,
  "status": "success"
}
```

异常日志额外包含 `error.type`、`error.message` 和 `error.stack_trace`。标准库日志调用仍可使用字符串消息，但新代码优先传递 `extra={"event": ..., ...}`。

### 5.2 输出与采集

- 控制台输出 JSON，便于 Docker 和进程管理器采集。
- 同时写入按大小轮转的 `backend/logs/app.jsonl`，本地开发也能接入采集器。
- Filebeat/Elastic Agent 读取 JSONL 并写入独立 `industry-assistant-logs-*` data stream。
- 只有需要字段转换和路由时才经过 Logstash；默认链路允许 Filebeat 直写 Elasticsearch。
- 日志索引不得与业务搜索索引共用名称或生命周期策略。

### 5.3 脱敏

日志禁止记录：

- Authorization、Cookie、API Key、数据库密码。
- 完整 Prompt、Response、用户 Query、网页全文和知识库文档。
- 图像 Base64、向量、完整检查点 JSON。

日志允许记录：长度、数量、SHA-256 摘要、域名、状态、耗时、模型、Token、错误类型和有限长度的安全摘要。

## 6. OpenTelemetry 与 Langfuse

### 6.1 Trace 结构

每个 Run 建立根 Trace：

```text
deep_research.run
├── request.accept
├── outline.plan
│   └── architect.call_llm
├── outline.await_approval
├── research.chapter.{section_id}
│   ├── web_search
│   ├── milvus_retrieval
│   ├── deep_read
│   └── fact_extraction
├── analysis
│   ├── data_extraction
│   ├── knowledge_graph
│   ├── code_execution
│   └── chart_generation
├── writing.chapter.{section_id}
│   └── writer.call_llm
├── critic.review
│   └── critic.call_llm
├── supplementary_research / revision
├── checkpoint.persist
└── report.complete
```

### 6.2 Observation 类型

- Agent 决策：`agent`
- LLM 调用：`generation`
- 网络搜索、代码执行：`tool`
- Milvus、PostgreSQL 检索：`retriever`
- 阶段编排：`span` 或 `chain`
- 质量审核：`evaluator`

LLM Generation 记录模型、温度、最大 Token、输入/输出 Token、总 Token、耗时、重试和成本。Prompt/Response 是否记录由配置控制，默认开发环境记录脱敏内容，生产环境仅记录摘要和长度。

### 6.3 故障降级

- Langfuse 或 OTLP 不可用不得阻塞报告生成。
- Trace 异步批量上报，关闭进程时执行有限时长 flush。
- 上报失败写结构化 warning 并增加 Prometheus 失败计数。
- Trace 初始化失败时使用 no-op tracer，业务逻辑保持可运行。

## 7. Prometheus 指标

### 7.1 HTTP 与进程

- `http_requests_total{method,route,status}`
- `http_request_duration_seconds{method,route}`
- Python 进程默认 CPU、内存和 GC 指标
- 容器指标由 cAdvisor 提供

### 7.2 深度研究

- `deep_research_runs_total{trigger,status}`
- `deep_research_inflight`
- `deep_research_phase_duration_seconds{phase,status}`
- `deep_research_iterations_total{outcome}`
- `deep_research_checkpoint_saves_total{status}`
- `deep_research_resume_total{reason}`
- `deep_research_sse_disconnects_total`
- `deep_research_events_persist_failures_total`

### 7.3 LLM、RAG 与工具

- `llm_requests_total{model,agent,status}`
- `llm_request_duration_seconds{model,agent}`
- `llm_tokens_total{model,agent,direction}`
- `llm_retries_total{model,agent}`
- `retrieval_requests_total{source,status}`
- `retrieval_duration_seconds{source}`
- `retrieval_results_total{source}`
- `tool_calls_total{tool,status}`
- `tool_duration_seconds{tool}`
- `research_event_queue_size`

所有 Label 必须来自有限枚举或规范化路由。禁止使用 `session_id`、`research_id`、`run_id`、`trace_id`、用户 ID、Query、URL、错误消息和集合名作为 Label。

## 8. 业务事件流水

### 8.1 `research_runs`

字段：

- `id`：UUID，等于 `run_id`
- `research_id`：UUID
- `session_id`：现有聊天会话 UUID
- `user_id`：用户 UUID
- `trigger`：`initial`、`outline_approved`、`resume`
- `status`：`running`、`awaiting_approval`、`completed`、`failed`、`cancelled`
- `current_phase`
- `trace_id`
- `started_at`、`finished_at`
- `error_type`、`error_message`
- `metadata_json`

### 8.2 `research_events`

字段：

- `id`：UUID
- `run_id`、`research_id`、`session_id`
- `sequence`：Run 内严格递增整数
- `event_type`
- `phase`、`agent`
- `status`
- `trace_id`、`span_id`
- `duration_ms`
- `payload_json`：脱敏后的有限载荷
- `occurred_at`

`(run_id, sequence)` 唯一，事件只追加不更新。检查点仍是恢复用快照，事件表是审计和时间线来源。

### 8.3 必须记录的事件

```text
QUERY_RECEIVED
OUTLINE_GENERATION_STARTED
OUTLINE_GENERATED
OUTLINE_APPROVAL_REQUIRED
OUTLINE_APPROVED
PHASE_STARTED
PHASE_COMPLETED
LLM_CALL_COMPLETED
SEARCH_STARTED
SEARCH_COMPLETED
RETRIEVAL_COMPLETED
FACTS_EXTRACTED
ANALYSIS_COMPLETED
CODE_EXECUTION_COMPLETED
CHARTS_GENERATED
SECTION_WRITTEN
CRITIC_REVIEWED
SUPPLEMENTARY_RESEARCH_REQUESTED
CHECKPOINT_SAVED
STREAM_DISCONNECTED
RUN_RESUMED
REPORT_COMPLETED
RUN_FAILED
RUN_CANCELLED
```

事件持久化失败不得让研究主流程失败，但必须输出 error 日志、增加失败指标，并将 Run 标记为 `observability_degraded=true`。

## 9. 查询 API

新增只读接口，全部要求登录并校验会话所有权：

- `GET /research/runs?session_id={id}&limit=20&cursor={cursor}`
- `GET /research/runs/{run_id}`
- `GET /research/runs/{run_id}/events?limit=200&cursor={cursor}`
- `GET /research/sessions/{session_id}/timeline`

集合接口采用游标分页。响应不返回完整 Prompt、LLM Response、网页全文、图像 Base64 和向量。不存在返回 404，无权访问返回 403，非法游标返回 400。

## 10. 前端研究时间线

现有“过程报告”增加“运行诊断”区域：

- 顶部显示 Run 状态、当前阶段、开始时间、总耗时、Trace ID 和是否降级。
- 时间线按阶段折叠，显示 Agent、状态、耗时、结果数量和错误。
- LLM 节点显示模型、Token、耗时和重试次数，不显示未授权的完整 Prompt。
- 搜索节点显示查询摘要、来源类型、结果数和失败原因。
- 提供“在 Langfuse 查看”链接和可复制的 `trace_id/run_id/session_id`。
- 刷新后从 PostgreSQL 事件流水恢复，不依赖当前 SSE 连接。
- 运行中通过现有 SSE 立即更新，并定期与事件 API 对账。

## 11. 基础设施

在独立 observability Compose Profile 中增加：

- Kibana，与现有 Elasticsearch 版本一致。
- Filebeat 或 Elastic Agent。
- Logstash，可选 Profile，仅在需要转换时启用。
- Prometheus。
- Grafana，并预配置 Prometheus 和 Elasticsearch 数据源。
- Alertmanager。
- cAdvisor、PostgreSQL Exporter、Redis Exporter。

Milvus 使用自身指标端点；Langfuse 保留现有 ClickHouse、PostgreSQL、Redis、MinIO 部署。

监控容器不应默认暴露到非本机地址。密钥通过环境变量或 Docker Secret 传入，配置文件不得提交真实密钥。

## 12. 默认仪表盘与告警

### 12.1 Grafana 大盘

- API Overview：QPS、P50/P95/P99、5xx、活跃请求。
- Deep Research：运行量、成功率、阶段耗时、恢复、检查点失败、SSE 断开。
- LLM & RAG：模型调用、Token、延迟、失败率、检索耗时和结果数。
- Dependencies：PostgreSQL、Redis、Milvus、Elasticsearch 和容器资源。

### 12.2 初始告警

- 5 分钟 5xx 比例大于 5%。
- 最终检查点保存失败立即告警。
- 研究 Run 在同一阶段超过 30 分钟。
- LLM 同一模型连续超时 3 次或 10 分钟失败率超过 20%。
- 搜索 10 分钟失败率超过 20%。
- 事件持久化连续失败。
- PostgreSQL、Redis、Milvus 或 Elasticsearch 不可达。
- 磁盘使用率超过 85%。

Alertmanager 默认支持邮件。企业微信通过通用 Webhook 指向企业微信机器人适配服务，仓库只提供模板和环境变量，不提交真实地址。

## 13. 保留与隐私

- 应用 INFO 日志默认 30 天，ERROR 日志 90 天。
- Prometheus 本地指标默认 30 天。
- 研究事件与业务会话遵循业务数据保留策略，不随日志自动删除。
- Langfuse开发环境保留脱敏输入输出；生产环境默认只记录摘要和 Token，可通过受控配置开启采样。
- 用户删除会话时，同步删除关联 Run/Event；Langfuse删除使用异步清理任务并记录结果。

## 14. 测试策略

为控制风险，实施拆成四个连续、可独立验证的交付单元，但共享本设计中的标识与数据契约：

1. **观测基础**：Context、JSON Log、请求中间件、`/metrics` 和基础测试。
2. **研究追踪**：Run/Event 数据模型、Langfuse、Agent/LLM/RAG/Tool 埋点和查询 API。
3. **监控基础设施**：日志采集、Kibana、Prometheus、Grafana、Alertmanager、Exporter 和预置规则。
4. **用户可见时间线**：前端诊断视图、刷新恢复、Langfuse 跳转和端到端测试。

每个单元完成后都必须运行已有全量测试，并提交独立 commit；后续单元不得通过复制关联 ID 或另建平行数据模型绕过第一单元的公共接口。

### 14.1 单元测试

- Context 创建、继承、清理和并发隔离。
- JSON 日志字段、异常格式和敏感字段脱敏。
- Metrics 计数、直方图和高基数 Label 防护。
- Event Sanitizer、序列递增和持久化降级。
- Langfuse 启停和 no-op 降级。

### 14.2 集成测试

- FastAPI 请求响应头和 `/metrics`。
- PostgreSQL Run/Event 写入、权限和游标分页。
- 初始规划、审批继续、恢复共享 `research_id`，但使用不同 `run_id/trace_id`。
- 最终检查点必须在 REPORT_COMPLETED 前成功保存。
- Langfuse不可达时报告仍能完成，且指标与日志标记降级。

### 14.3 端到端测试

- 从 Query 到大纲、批准、报告完成，时间线可刷新恢复。
- 断开 SSE 后恢复，旧 Run 结束、新 Run 标记 `resume`。
- 失败工具调用在时间线、日志和指标中使用同一 Trace Context。
- 前端不得展示敏感 Prompt 或凭据。

## 15. 验收标准

1. 输入任意深度研究 Query 后，可按 `session_id` 找到所有 Run。
2. 每个 Run 在日志、Langfuse、PostgreSQL事件和API响应中使用同一关联上下文。
3. Langfuse中可见嵌套 Agent、LLM、Retriever、Tool和Evaluator节点。
4. Kibana可通过 `trace_id` 查询该Run的全部应用日志。
5. Grafana可查看研究成功率、阶段P95、LLM Token和工具失败率。
6. 最终检查点失败能够触发Alertmanager告警。
7. 刷新前端后仍能看到完整研究时间线。
8. Langfuse、Elasticsearch或Prometheus任一不可用都不阻塞报告生成。
9. Prometheus中不存在以用户、会话、Run、Trace、Query或URL为Label的时间序列。
10. 自动化测试和生产构建全部通过，配置中不存在真实密钥。
