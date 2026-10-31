# DeepResearch Multi-Agent 源码—简历对齐审计

审计日期：2026-10-31  
审计范围：在线 V2 主链、长期记忆、可靠性、评测与可观测、SFT/QLoRA 子项目。  
结论口径：只有源码、自动化测试和可复核产物同时支持的数字，才视为可直接写入简历。

## 1. 总结

当前仓库已经具备五条简历主线所需的大部分工程骨架：六 Agent AsyncIO 状态机、
Web/LocalRAG/Text2SQL 多源检索、Fact/Experience 记忆、重试/熔断/降级/DLQ、
PostgreSQL JSONB checkpoint、规则评测与五维 Judge、训练数据流水线、Qwen2.5-7B
QLoRA、灰度工具路由均有真实源码。

但现阶段不能原样使用简历中的全部强表述和数字：

- `89%` 首轮通过率、`93%` 工具恢复率、任务失败率 `<5%`、`3.62→4.31`、引用覆盖率
  `96%`、`5000+` 轨迹和 `76%→92%` 均没有本仓库可复核的 Manifest/评测报告。
- Critic 目前信任模型返回的 `verdict`，没有在代码层强制“低分不得通过”；空或非法审核结果也
  可能被主循环当作可结束路径。
- Fact/Experience 已有版本、状态与手工归档，但没有按访问频次/时效自动执行冷热迁移；
  Experience 的 `strategy` 仍是固定模板，不是从 Critic 反馈结构化归纳出的策略。
- 后端追踪主要由 Langfuse observation 封装提供；显式 OTLP Provider 只出现在工具路由服务，
  还不足以严谨表述为“后端到推理服务的 OpenTelemetry 全链路 Trace”。
- `docs/architecture.md` 已落后于源码：把 ResearchState 写成 dataclass、把 CodeWizard 写成
  修订 Agent，并称 Elasticsearch 未被应用层使用；实际源码分别是 TypedDict、LeadWriter 修订、
  Milvus + Elasticsearch/BM25 混合检索。

因此，当前状态是“核心能力基本对齐，量化证据尚未对齐”。

## 2. 逐条对齐矩阵

| 简历主张 | 状态 | 源码证据 | 缺口 / 风险 |
|---|---|---|---|
| 六个专家 Agent + 自研 AsyncIO 状态机 | 已实现 | `backend/app/service/deep_research_v2/graph.py` 初始化六角色并由 `_run_state_machine` 驱动；`state.py` 定义共享状态 | `run_agent_with_streaming` 会记录后吞掉 Agent 异常，后续阶段仍可能继续 |
| Web、知识库、数据库并行检索 | 已实现 | `agents/scout.py:1043-1154` 通过统一工具契约选择 Web/LocalRAG/Text2SQL；同一查询的多个调用由 `asyncio.gather` 并发执行 | 查询之间仍串行；补搜路径目前直接调用 Web Search，未复用多源路由 |
| Milvus + BM25 / RRF | 已实现 | `retrieval/hybrid_retriever.py` 并发两路并支持单路降级；`retrieval/fusion.py` 执行加权 RRF | `docs/architecture.md` 仍错误声称 ES 未使用 |
| 带引用研究报告 | 基本实现 | Writer prompt、事实与 references 会进入报告生成和最终 SSE | 没有报告生成后的“主张—引用—原文”一致性校验，当前只能保证带链接，不能保证每条引用真实支撑主张 |
| 覆盖不足自动补搜、其他问题定向修订 | 已实现 | `agents/critic.py:247-293` 按 missing_source/incomplete/outdated 和 missing_aspects 路由；状态机回到 Scout 或 Writer | 路由基于 LLM 问题标签，不是确定性引用覆盖阈值 |
| 低分报告强制修订 | 未完全实现 | Prompt 约定 7 分通过 | `critic.py:218-243` 只检查 `verdict`；模型若返回低分 + pass，源码仍会通过；审核解析失败也没有 fail-closed |
| 最多 3 轮收敛 | 默认请求对齐 | API 默认 `max_iterations=3`，状态机按 iteration 控制 | GET 接口允许 1–5，底层配置默认 1；最后一次修订后可能未经再次审核就被标记完成，需要统一轮次语义 |
| 首轮通过率约 89% | 无证据 | `critic.review_completed` 已记录 iteration，可作为统计数据源 | 没有基准集、统计脚本和版本化报告 |
| Fact / Experience 双层记忆 | 已实现 | `models/research_memory.py`、`service/research_memory/` | Experience 策略为固定模板，尚未真正从 Critic 反馈抽象策略 |
| 规划前混合召回 | 已实现 | `graph.py:340-366` 在规划前召回；`research_memory/service.py` 按向量、BM25、时效、置信度融合 | 索引状态与数据库状态不是事务一致；归档/失效后索引文档不会同步更新 |
| 去重合并、置信度版本演化 | 已实现 | `repository.py` 使用 canonical key、content hash、版本链和置信度冲突规则 | 当前是结构化键 + 精确内容哈希，不是语义级事实合并 |
| 冷热归档 | 部分实现 | 模型有 status、last_accessed_at、access_count，API 支持手工 archive | 没有访问计数更新、自动归档策略、冷数据恢复和定时任务 |
| 重试、熔断、降级、DLQ | 已实现 | `resilience/executor.py` 统一组合；BaseAgent LLM 调用和 Scout 工具调用已接入 | 需要端到端故障注入证明，而不只是单元测试 |
| 幂等键、优雅取消、级联超时 | 基本实现 | Scout 工具调用有 SHA-256 幂等键；Redis 取消标志、SSE 断连清理、run deadline + child timeout 已实现 | LLM 调用未提供幂等键；Agent 异常被吞掉会削弱失败语义 |
| PostgreSQL JSONB 全量状态、事件审计、断点恢复 | 已实现 | `ResearchCheckpoint.state_json/ui_state_json`、`ResearchRun/ResearchEvent`、phase dispatcher 与 resume API | checkpoint 是阶段级快照，不是每个工具调用后的精确重放；“断点回放”更准确地说是“从最近阶段快照恢复” |
| 工具恢复率约 93%、任务失败率 <5% | 无证据 | 有 Prometheus 指标和 `evals/fault_injection.py` 雏形 | fault injection 只有 3 个场景定义，没有运行报告；没有线上样本窗口、分母和版本信息 |
| 规则指标 + 五维 LLM-as-Judge + Bootstrap 95% CI | 源码已实现 | `backend/evals/` 提供规则、五维 Judge、组合评分、Bootstrap、p-value、Cohen's d | CLI 当前只构造无 Judge 的 Runner，未把 Judge provider 接入正式流水线；无评测产物 |
| 事实性 3.62→4.31、引用覆盖率 96% | 无证据且量纲未对齐 | Judge 是 0–10，规则/组合分是 0–1 | `3.62→4.31` 看起来是 5 分制，和当前实现口径不一致；必须先固定量纲、基准集和报告 Schema |
| OpenTelemetry + ELK 日志关联 + 分级告警 | 部分实现 | 结构化日志携带 request/run/trace/session id；ELK、Prometheus、Alertmanager 配置存在；工具路由服务有 OTLP Provider | 后端未显式配置 OTLP Provider/FastAPI instrumentation；Langfuse Trace 与 OTLP 链路的边界需统一 |
| 教师合成、过滤、规范化、去重、难度分桶 | 已实现 | `training/scripts` 与 `training/src/data` 有完整流水线，SimHash 去重、父种子分组切分和 Manifest 已实现 | 当前仓库没有 reviewed seeds、生成后的 routing-v1 数据和 Manifest |
| Qwen2.5-7B QLoRA + 门禁 + 灰度 | 已实现 | NF4 QLoRA、held-out eval、package gate、内部路由服务、稳定哈希灰度和确定性回退均有源码 | release 包实际不包含数据集 Manifest；当前无 Adapter、基线/候选评测与 release 产物 |
| 5000+、工具选择 76%→92% | 无证据 | README 明确要求以 Manifest 和评测 JSON 为准 | 当前 `training/data` 只有说明文件，`training/releases` 只有 `.gitkeep`；相邻旧训练项目也明确声明旧分数作废 |

## 3. 当前可以安全使用的简历版本

在没有重新跑出量化报告前，建议先使用下面的可辩护口径：

> 面向企业复杂决策中信息分散、调研慢和结论难追溯的问题，设计并开发多 Agent 深度调研系统；
> 调度专家 Agent 完成 Web、企业知识库与只读数据库检索，生成带来源链接的研究报告，并通过
> checkpoint、事件审计和故障收敛机制支持长链路任务恢复与验收。

- **多智能体编排：** 基于自研 AsyncIO 状态机调度 ChiefArchitect、DeepScout、
  DataAnalyst、CodeWizard、CriticMaster、LeadWriter 六个专家 Agent，串联规划、检索、
  分析、写作和评审；Critic 将证据缺失路由至补搜，其余问题路由至定向修订，默认最多 3 轮。
- **长期记忆管理：** 构建 Fact/Experience 双层长期记忆，分别保存带来源与置信度版本的事实和
  历史评审经验；规划前融合向量、BM25、时效与置信度召回，并支持事实去重、冲突版本链和归档状态管理。
- **依赖容错与可恢复：** 将 LLM 与检索工具调用纳入重试、熔断、降级和死信队列，配套工具幂等键、
  级联超时、优雅取消；以 PostgreSQL JSONB 保存阶段快照，并通过研究事件账本和 resume API
  从最近检查点恢复。
- **评测与可观测：** 实现规则指标、五维 LLM-as-Judge、Bootstrap 95% CI、p-value 与
  Cohen's d 的离线评测模块；以 Prometheus 指标、结构化关联日志、ELK 和 Alertmanager
  追踪运行状态和分级告警。
- **SFT 数据飞轮：** 实现教师模型轨迹合成、脱敏、Schema 校验、质量过滤、SimHash 去重、
  难度分桶与按父种子隔离的数据流水线；基于 Qwen2.5-7B 构建 NF4 QLoRA、held-out
  评测门禁、Adapter 打包和稳定哈希灰度发布链路。

这版只陈述已存在且可从源码解释的能力，不声明尚未生成证据的结果数字。

## 4. 要保留原简历强表述，必须完成的升级

### P0：先修正确性和强语义

1. 为 Critic 输出建立 Pydantic 契约；代码层同时要求 `quality_score >= 7`、无 critical issue
   且 `verdict=pass` 才允许完成。解析失败必须重试或失败，不能静默完成。
2. 明确定义“3 轮”是 3 次评审还是 3 次返工；每次返工后必须有终审，达到上限仍未通过时状态应为
   `completed_with_risk` 或 `failed_quality_gate`，不能伪装为正常完成。
3. Agent 异常必须向上传播并落失败 checkpoint；禁止 `run_agent_with_streaming` 记录后吞掉异常。
4. 补搜路径复用统一工具路由，使 Web/LocalRAG/Text2SQL 在首次研究和返工阶段保持同一契约。
5. 修正 `docs/architecture.md` 与真实源码的 TypedDict、LeadWriter 修订、混合检索和 OTel 边界。

### P1：建立数字证据流水线

1. 增加版本化 benchmark manifest：数据集版本、case id、模型、Prompt、工具 Schema 摘要、Git
   revision（当前目录没有 `.git`，升级前应恢复版本来源）、随机种子和运行时间。
2. 从 `critic.review_completed` 统计首轮通过率，固定分母为“成功进入首次 Critic 的任务数”。
3. 扩展 fault injection，至少覆盖 timeout、429、5xx、连接失败、熔断打开、主/备同时失败、
   DLQ replay、SSE 断连和 checkpoint 恢复；输出工具恢复率与任务失败率及 Bootstrap CI。
4. 给 `run_research_eval.py` 接入真实 Judge provider；报告同时输出原始 0–10、折算 1–5 和 0–1
   分，避免 `3.62→4.31` 量纲不明。
5. 引用指标至少拆成 claim citation coverage、URL validity、source entailment 三项；`96%` 只能引用
   明确命名的 coverage 字段。
6. 生成脱敏的 `docs/evidence/*.json` 汇总报告，保存原始产物路径和 SHA-256；简历数字只从这些
   报告自动生成，禁止手填。

### P1：补齐长期记忆闭环

1. 检索命中时原子更新 `last_accessed_at/access_count`。
2. 增加基于时效、访问频次和置信度的 hot/warm/cold 策略、定时归档与按需恢复。
3. 数据库状态变化同步到 Milvus/ES 索引，避免 invalidated/archived 记忆继续出现在候选集合。
4. 从 Critic issue、采取动作和前后评分生成结构化 Experience strategy，替换固定策略模板。

### P2：补齐真正的端到端 OpenTelemetry

1. 后端显式初始化 OTLP TracerProvider，并对 FastAPI、HTTPX/Requests、PostgreSQL、Redis
   按需插桩；继续传播 W3C `traceparent` 到工具路由服务。
2. 统一 Langfuse observation 与 OTel span 的 trace id，结构化日志只写同一个 trace id。
3. 增加一次跨服务验收：HTTP 请求 → 状态机 → Agent LLM → Tool Router → 依赖调用可以在
   Trace UI 中串成一条链，并能用相同 id 在 ELK 中查到日志。

### P2：跑出 SFT 真产物

1. 准备并评审种子问题，实际生成不少于目标数量的轨迹；以 `routing-v1/manifest.json` 的
   `valid_count` 作为唯一数量来源。
2. 在同一 held-out test 上分别评估 base 与 Adapter，生成 baseline/candidate JSON；只有门禁通过
   才允许打包。
3. release 包加入 dataset manifest 或其 SHA-256 和不可变存储地址；保存 Adapter、Schema、
   训练 manifest、评测报告的摘要关系。
4. 只有当候选报告真实给出 `0.76→0.92` 时，才恢复简历中的 `76%→92%`。

## 5. 建议的数字证据清单

| 简历数字 | 唯一证据产物 | 最低必要字段 |
|---|---|---|
| 首轮通过率 89% | `docs/evidence/critic-benchmark-v1.json` | total_first_reviews、passed_first_review、rate、95% CI、dataset/model/prompt version |
| 工具恢复率 93% | `docs/evidence/fault-injection-v1.json` | scenario、attempts、recovered、rate、95% CI、policy version |
| 任务失败率 <5% | `docs/evidence/run-reliability-v1.json` | window、eligible_runs、failed_runs、rate、排除规则 |
| 事实性 3.62→4.31 | `docs/evidence/report-eval-v1.json` | scale、baseline/candidate、paired case ids、CI、p-value、Cohen's d、judge model |
| 引用覆盖率 96% | 同上 | coverage 定义、claim_count、cited_claim_count、URL/entailment 是否另计 |
| 数据 5000+ | `training/data/routing-v1/manifest.json` + 汇总摘要 | raw/valid/rejected、train/dev/test、difficulty、schema hash |
| 工具选择 76%→92% | `training/reports/baseline.json`、`candidate.json` | same test hash、total_steps、tool_selection_accuracy、schema validity、gate |

## 6. 本次验证结果

- Backend Ruff：通过。
- Training Ruff：通过。
- Training pytest：`6 passed`。
- Backend pytest：当前环境缺少 `openai` 包，在加载 `tests/conftest.py` 时中止；不是测试失败，
  但也不能据此宣称后端测试通过。仓库内可检索到约 292 个测试函数。
- Frontend：当前目录没有 `node_modules`，本次未执行 lint/typecheck/test。
- 当前目录不是 Git checkout，无法给本次审计绑定 commit SHA。

