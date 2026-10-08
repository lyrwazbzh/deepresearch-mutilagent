# 集中测试约定

## 0. 定位

本文定义工程测试的分层、测试基座、Fixture、基础设施隔离和 Pull Request 门禁。

```text
Unit        → 纯逻辑正确性
Contract    → 单个边界的协议一致性
Integration → 真实基础设施与跨组件行为
E2E         → API、异步执行和持久化结果组成的确定性主链
```

具体业务场景由对应模块文档维护；质量指标由 [`evaluation.md`](evaluation.md) 维护；Route/Tool Decision 数据与模型门禁由 [`data_flywheel.md`](data_flywheel.md) 维护；部署、迁移、备份恢复和生产 smoke test 由 [`deployment.md`](../04_platform/deployment.md) 维护。

---

## 1. 测试目录

```text
tests/
├─ unit/
├─ contract/
├─ integration/
├─ e2e/
└─ fixtures/
   ├─ documents/       # Document ingestion 文件
   ├─ tabular/         # SQL/Data 分支使用的 XLSX、CSV、Parquet
   └─ providers/       # 外部 Provider 的确定性请求/响应序列
```

`integration/` 按业务能力组织测试；PostgreSQL(+pgvector)、Redis、MinIO、Elasticsearch+IK 和 Neo4j 由统一测试基座提供。

---

## 2. 四层测试职责

### 2.1 Unit

验证无 I/O 的纯函数、Schema、Policy、状态转换、预算计算、错误分类和确定性序列化。Unit 只使用内存输入。

### 2.2 Contract

验证一个边界两端对同一协议的理解一致，包括：

- Port 与 Adapter；
- Tool Input / Output；
- Provider 请求、响应和错误归一化；
- HTTP 请求、响应和 Event Schema。

Contract Test 使用本地协议 Stub 或 Fake Server 驱动真实 Adapter，并以同一组合同样例约束实现；跨存储事务、异步调度和完整业务主链分别由 Integration 与 E2E 验证。

### 2.3 Integration

使用 Testcontainers 启动与生产同类型的 PostgreSQL(+pgvector)、Redis、MinIO、Elasticsearch+IK 和 Neo4j，验证：

- migration、约束、事务、claim/CAS 与幂等；
- Repository 的 scope 查询和并发行为；
- ES mapping、BM25/kNN 检索与索引发布；
- MinIO 临时对象发布和 Workspace 文件生命周期；
- Outbox consumer handler、Document ingestion、Export 与 Task KG 的跨组件行为。

Integration 可以直接调用 consumer handler，以精确构造重复投递、迟到提交和故障恢复条件。

### 2.4 E2E

启动真实 API、`outbox-dispatcher`、业务 Worker、Graph Runtime 和同栈基础设施；外部 Model、Web 与 MCP Provider 使用确定性协议服务。

```text
通过 API 创建资源
  → 等待 PostgreSQL 权威状态或 Event
  → 验证 ResearchState / Workspace / Document / Audit / Trace
```

E2E 覆盖可重复的最小主链。状态等待以权威 PG 状态或 Event 轮询为准。

三条入口路径至少各有一条确定性 E2E：

```text
direct  → RouteNode → ChiefArchitect → assistant Message
focused → RouteNode → 单个执行 Agent → bounded Tool/ReAct → assistant Message
deep    → Plan → Web/RAG/SQL → Merge → AnalysisDecision/Compute?
        → Coverage → Critic → Writer → Schema/Citation Validation → Finalize
```

Contract/Integration 另外固定三条边界：DataAnalyst 不产生 AnalysisRequest；Post-Merge Analysis Decision 是唯一生产者；Critic 的 retrieve channel 只接受 `web|rag|sql`。Coverage 只做结构断言，Critic 的语义结论使用冻结模型响应验证路由结果。

---

## 3. Fake Provider 与真实调用边界

允许的外部 Fake：

```text
FakeChatProvider
FakeEmbeddingProvider
FakeRerankerProvider
FakeWebProvider
FakeMCPServer
```

Fake 位于外部协议边界，生产调用链保持完整：

```text
Agent
  → ModelRuntime
  → Provider Adapter
  → Fake Chat / Embedding / Rerank Provider

Agent
  → ToolRuntime
  → Web / MCP Adapter
  → Fake Web Provider / Fake MCP Server
```

因此测试仍经过 Model Binding、ModelRuntime、ToolRuntime、真实 Adapter 和错误归一化。Fake MCP Server 实现真实 MCP 协议；SQL/Data Integration 使用真实 PostgreSQL 或 DuckDB；Sandbox Integration/E2E 使用真实隔离 Runner。

Provider fixture 按版本保存请求匹配条件、流式片段、token usage、错误、延迟和 attempt 序列，使 retry、fallback 与 Circuit Breaker 状态转换可重复验证。

---

## 4. 确定性测试基座

公共测试基座提供：

```text
TestClock                 # 固定或显式推进时间
DeterministicIdFactory    # 生成可预测的业务 ID
FixedRandomSeed           # 固定随机采样
ProviderAttemptSequence   # 声明每次外部调用的结果
InMemorySpanExporter      # 捕获 Unit / Contract Trace
```

Unit 与 Contract 通过推进 `TestClock` 验证 timeout、lease 和重试，不等待真实时间。Integration 与 E2E 的业务 ID、Provider 响应和轮询 deadline 均由测试基座显式设置。

这些控制项只属于测试 Harness；生产 Schema、ResearchState、ExecutionContext 和公开 API 保持生产合同。

---

## 5. 基础设施生命周期与隔离

```text
test session
  → 启动一组生产同栈 Testcontainers
  → 从 0 执行 migration / index template / bucket 初始化
  → health / readiness
  → 每个 test 创建唯一 tenant / user / task / doc 等业务 ID
  → 在共享物理存储中执行
  → 按 Resource Manifest 清理本次资源
  → session 结束销毁容器
```

同一测试会话共用物理 PostgreSQL schema、ES index、MinIO bucket、Neo4j database 和 Redis 实例，以便真正验证 Repository filter、对象前缀和索引 scope。Harness 通过 Resource Manifest 与内部 `test_scope` 跟踪清理，产品合同保持不变。

租户隔离用例同时覆盖同租户同用户、同租户异用户和跨租户访问，并验证不同用户的数据确实位于同一物理存储中。

---

## 6. 异步执行测试

| 层 | 调用方式 | 验证重点 |
|---|---|---|
| Unit | 直接调用纯逻辑 | 状态转换、分类、幂等键计算 |
| Contract | 调用单个 Port / Adapter | 消息、HTTP、Provider 与 Tool 合同 |
| Integration | 直接调用 consumer handler | claim/CAS、重复投递、NodeRun 重试、Ingestion 与 Export |
| E2E | 启动 Redis、`outbox-dispatcher` 和真实 Worker | 调度、Pause/Resume/Cancel、恢复、Event 与最终持久化结果 |

Outbox 的 at-least-once 发布、Worker 重复唤醒、NodeRun attempt、Document ingestion generation、Export 和审计写入均在对应模块的 Integration/E2E Case 中验证。

---

## 7. Observability 与 Audit 测试基座

```text
Unit / Contract → InMemorySpanExporter
Integration     → BatchSpanProcessor + PG Exporter
E2E             → observability_spans + audit_logs
```

断言口径由 [`observability.md`](observability.md) 定义，包括一次 Worker 激活对应一个 `research.run`、同一 `node_run_id` 的 attempt 关系、Tool Event、暂停恢复产生多个运行 Trace、Exporter 故障不影响业务提交，以及 Audit 的 `action + result` 合同。

---

## 8. CI 门禁

```text
Pull Request
  → format / lint
  → typecheck
  → unit
  → contract
  → integration（真实同栈容器）
  → deterministic e2e（Fake 外部 Provider）
  → 文档链接 / Schema / migration 一致性检查
```

确定性 Pull Request 门禁使用 Fake Provider；外部真实 Provider 的连接检查在受控环境中按需运行。镜像构建、migration compatibility、Compose smoke、readiness、队列、SSE、上传和备份恢复属于部署流水线，见 [`deployment.md`](../04_platform/deployment.md)。

---

## 9. Fixture、Evaluation 与 Flywheel 数据边界

```text
tests/fixtures
  → 工程测试输入；小、确定、随代码版本维护

Evaluation Dataset
  → RAG、Research LTM、报告质量和 Skill 评测；不可变版本与 EvalRun 关联

Flywheel Dataset Snapshot
  → Route / Tool Decision 训练、验证和离线模型门禁
```

工程 Fixture 只服务工程回归；Evaluation Dataset 和 Flywheel Snapshot 使用各自文档定义的存储、版本与生命周期。

---

## 10. 验收标准

```text
□ unit / contract / integration / e2e 可独立运行
□ Fake 位于外部协议边界，测试经过真实 ModelRuntime、ToolRuntime、RetrievalGateway 与 Adapter
□ Testcontainers 从空环境执行生产 migration 和初始化
□ 租户隔离在共享物理存储中验证
□ 时间、ID、随机性、Provider attempt 和 Trace 可确定控制
□ Integration 可直接验证 handler 的 claim/CAS 与重复投递
□ E2E 通过 PG 状态或 Event 等待真实异步主链
□ direct/focused/deep 三条路径以及 Critic 前置、Writer 后确定性校验顺序均有回归用例
□ AnalysisRequest 单一生产者、Coverage/Critic 分工和 Critic channel 枚举有合同测试
□ Pull Request 工程门禁与 Deployment Pipeline 职责分离
□ 模块测试 Case 只在对应 Source of Truth 维护
```
