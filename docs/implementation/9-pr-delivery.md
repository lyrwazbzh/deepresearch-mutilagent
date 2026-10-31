# DeepResearch 九组依赖化改造

这份文档定义代码拆分边界。九组按顺序合入；每组只依赖其前置组。仓库根目录负责在线研究，`training/` 负责数据、训练、
评测、Adapter 发布与推理服务。

## 1. 工具契约与 AsyncIO 运行时

- 权威 Schema：`contracts/deepresearch_tools.v1.json`。
- Python 契约：`backend/app/contracts/tool_routing.py`。
- 工具：`web_search`、`local_rag`、`text2sql`、`data_analysis`、`chart_generation`。
- 研究终止和 Critic 返工不属于模型工具，由 AsyncIO 状态机控制。
- `deep_research_v2/graph.py` 以 `_run_state_machine` 作为唯一 V2 执行入口。

## 2. 合成数据与质量流水线

- 位于 `training/`，标准消息使用 `assistant.tool_calls`/`tool.tool_call_id`。
- `expand_seed_questions.py` 扩展种子；`synthesize_routing_trajectories.py` 并发合成并可按
  `seed_id` 续跑。
- `build_dataset.py` 执行敏感信息脱敏、Pydantic/JSON Schema 校验、质量过滤、
  SimHash 近重复去重和按父种子分组切分。
- 输出 `train/dev/test.jsonl` 与统计 Manifest；最终数量只能从 Manifest 读取。

## 3. Milvus + BM25 混合召回

- 接口：`Retriever.search(RetrievalRequest) -> list[RetrievalHit]`。
- `HybridRetriever` 并发调用 Milvus 与 Elasticsearch，并以加权 RRF 融合；单路故障时
  使用另一条结果，两路同时失败才向上抛错。
- 文档写入同时写 Milvus 和 `knowledge_chunks_v1`；ES 写入失败进入
  `search_index_outbox`，调度器每 30 秒补偿。

## 4. 在线工具路由

- 内部 API：`POST /v1/tool-routing`，请求/响应严格使用第 1 组契约。
- Scout 对每个检索问题选择 Web/LocalRAG/Text2SQL；分析阶段选择 DataAnalyst/CodeWizard。
- Text2SQL 只接受服务端 `TEXT2SQL_DATABASE_PROFILES` 中的 profile id，不接受用户 DSN。
- `tool_decisions` 与 `research_tasks` 写入研究状态，供 checkpoint 和审计使用。

## 5. 依赖容错与可恢复

- `ResilientExecutor.execute` 组合级联超时、指数退避重试、Redis 熔断、候选降级、
  PostgreSQL 幂等结果和 DLQ。
- 表：`tool_executions`、`dead_letter_tasks`、`search_index_outbox`；迁移文件
  `backend/migrations/20261020_reliability.sql`。
- API：`GET /reliability/dead-letters`、`POST /{id}/replay`、`POST /{id}/resolve`。
- `asyncio.CancelledError` 始终向上传播，SSE 断连会取消在飞 Agent。

## 6. Fact/Experience 长期记忆

- 表：`fact_memories`、`experience_memories`；迁移文件
  `backend/migrations/20261020_research_memory.sql` 和
  `backend/migrations/20261031_memory_lifecycle.sql`。
- Fact 按 subject+predicate 生成 canonical key，内容哈希去重，冲突时保留版本链；
  低置信度冲突归档，高置信度新版本使旧版本失效。
- 规划前按向量、BM25、时间和置信度混合召回；命中后原子更新访问次数与最后访问时间。
- 报告完成后从多轮 Critic 评审中归纳触发条件、有效策略、问题画像和评分变化，而非保存
  固定模板；周期任务依据时效、访问频率和置信度进行冷热迁移，冷层高相关命中自动恢复。
- API：`GET /research-memories`、`/search`、`/{id}`、`/{id}/versions`，
  `POST /{id}/archive`。

## 7. 离线报告评测

- `backend/evals` 提供规则事实性、引用覆盖/有效性、主题完整度、五维 LLM-as-Judge、
  Bootstrap 95% CI 和成对回归门禁。
- `python backend/scripts/run_research_eval.py --input cases.jsonl --output results.jsonl`。
- Critic 在线事件记录为 `critic.review_completed`；通过必须同时满足响应结构有效、评分不少于
  7 分、模型判定通过、无 critical/major 问题且无缺失主题。达到最大轮次仍失败时进入
  `quality_gate_failed`，不得标记为正常完成。

## 8. Qwen2.5-7B 4-bit QLoRA

- `training/scripts/02_train.py` 默认 Qwen2.5-7B-Instruct、NF4 double quant、
  LoRA r=16/alpha=32，支持独立验证集、最佳 checkpoint 和断点恢复。
- `evaluate_tool_router.py` 计算工具选择、参数 JSON、参数 Schema、轨迹成功率；
  评测结果未达到门禁时不得生成发布包。
- 训练后产出 Adapter 与 `release-manifest.json`，包含 Schema/数据摘要和训练评测元数据。

## 9. 推理、灰度与可观测

- `training/src/serve/app.py` 暴露 `/healthz`、`/v1/model`、`/v1/tool-routing`、`/metrics`。
- 主项目用 `session_id` 稳定哈希灰度；Adapter 超时、低置信度、越权工具或非法参数时
  自动回退确定性路由。
- HTTP 调用传播 W3C Trace Context；主项目记录路由、重试、降级、熔断、DLQ、记忆指标，
  推理服务输出 Prometheus 指标并可上报 OTLP。

## 验收顺序

1. 同步 Schema 并比较摘要。
2. 执行两个项目的静态检查与单元测试。
3. 应用 PostgreSQL 迁移，启动 Postgres/Redis/Milvus/Elasticsearch。
4. 用小规模数据完成一次 synthesis → build → QLoRA smoke run → held-out eval。
5. 打包 release，先设置 `TOOL_ROUTER_GRAY_PERCENT=0` 验证规则基线，再按
   10% → 30% → 100% 扩大；任何门禁回归立即恢复为 0。
