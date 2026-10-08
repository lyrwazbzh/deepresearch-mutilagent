# DeepResearch-mutilagent：评测体系（Evaluation）

## 0. 定位

Evaluation 使用固定 Dataset、已解析配置和生产 Service，量化四类能力：

```text
RAG           → 检索与排序质量
Research LTM  → 长期记忆写入与跨 Task 召回质量
E2E Research  → 最终研究报告质量与交付成功率
Skill         → Skill 是否在正确场景激活，以及激活后是否改善任务结果
```

Evaluation 回答系统版本的整体表现；[`observability.md`](observability.md) 负责解释单次运行发生了什么，[`testing.md`](testing.md) 负责确定性的工程合同与回归门禁。SFT 离线门禁见 [`data_flywheel.md`](data_flywheel.md)。

---

## 1. Suite 与执行主链

长期维护四个 Suite：

```text
Evaluation
├─ RAG Eval
├─ Research LTM Eval
├─ E2E Research Eval
└─ Skill Eval
```

统一 Runner：

```text
加载不可变 Dataset + 解析 Suite Config
  → Suite-specific Preflight
  → 创建 EvalRun(status=running)
  → 创建隔离 eval_scope
  → 初始化 Resource Manifest
  → 通过生产 Service 装载 Fixture
  → 逐 Case 执行
  → 规则评分 / LLM Judge
  → 保存 eval_case_results
  → 汇总 metrics_summary
  → finally 按 Resource Manifest 清理
  → EvalRun completed | failed
```

Preflight 在创建评测资源前检查当前 Suite 使用的存储、模型和 Provider。基础依赖不可用时立即终止；单个 Case 失败时保存诊断并继续其余 Case。

---

## 2. 工程边界

### 2.1 复用生产 Service

```text
RAG Eval
  → Document ingestion → RetrievalGateway.search

Research LTM Eval
  → MemoryService.extract_batch / remember / recall

E2E Research Eval
  → 受限 Eval Tenant 上运行真实 Research Graph

Skill Eval
  → 生产 SkillRegistry / SkillRuntime + 对应 Agent / Tool 链
```

Fixture 通过生产 Service 写入，评测 Runner 只负责组织输入、调用、评分与结果持久化。

### 2.2 隔离与清理

每个 EvalRun 创建独立 `eval_scope`。Setup 同时维护本次实际创建的资源清单：

```text
ResourceManifest
  documents[]
  tasks[]
  workspace_paths[]
  memory_ids[]
  es_generations[]
  kg_namespaces[]
```

Teardown 只处理 Manifest 中登记的资源：

```text
RAG
  → 本次 Document、MinIO 对象与 ES generation

Research LTM
  → 本次 eval_scope 下的 research_memories

E2E Research
  → 本次 Task、Workspace、Document 与实际生成的 Task KG

Skill
  → 本次 Task、Workspace 与 Skill 使用的实际 Fixture
```

Teardown 位于 `finally`；异常退出后的残留仍可由 `eval_scope_id` 定位和清理。

### 2.3 Summary 与逐 Case 明细

每次运行同时保存汇总指标与 Case 结果。Case 使用 Dataset 中的稳定逻辑 ID，不依赖本次运行生成的数据库 ID：

```yaml
case_id: ltm_recall_017
expected_relevant_ids:
  - market_scope_preference
returned_ids:
  - output_language
  - research_method_validation
hit_at_5: false
```

Fixture Setup 建立逻辑 ID 与本次 `doc_id / chunk_id / memory_id / task_id` 的映射。指标下降时直接从失败 Case 定位检索、记忆或报告阶段。

---

## 3. RAG Eval

评测生产检索链：

```text
Query
  → Document tenant filter
  → BM25 + Vector
  → RRF
  → Rerank
  → Parent Expansion
  → Ranked Chunks
```

### 3.1 Dataset

```text
业务 Gold Dataset
  → 产品质量与发布判断

C-MTEB Chinese Retrieval 子集
  → 可选外部中文检索参照
```

C-MTEB 使用同一个 RAG Suite 和同一组指标，不形成独立 Suite，也不替代业务 Gold。它用于模型或检索配置的阶段性比较，不要求每次 CI 执行。

业务 Case 至少包含：

```text
case_id
query
relevant_doc_ids[]
relevant_chunk_ids[]
scope
```

### 3.2 指标

| 指标 | 含义 |
|---|---|
| Recall@5 | 前五个返回 Chunk 对 Gold relevant Chunks 的召回比例 |

```text
Case Recall@5
  = |Top5 returned chunk_ids ∩ relevant_chunk_ids|
    / |relevant_chunk_ids|

Suite Recall@5
  = 所有 Case Recall@5 的宏平均
```

Dataset Preflight 要求 `relevant_chunk_ids` 非空。`relevant_doc_ids` 只用于 Fixture 构造和 Document 级诊断，不进入 Recall@5 计算。

### 3.3 配置比较

长期保留两个可解释配置：

```text
BM25 baseline
Current production retrieval
```

生产配置的 Embedding、RRF、Rerank 和 Parent Expansion 参数进入 `config_snapshot`。其他临时组合通过独立 EvalRun 比较，不扩展长期配置矩阵。

---

## 4. Research LTM Eval

Research LTM 只评估生产 MemoryService 的业务输出：

```text
Write Eval
  → MemoryService.extract_batch / remember

Recall Eval
  → MemoryService.recall
```

### 4.1 Write Gate

Dataset 为每个输入标注 `should_write`：

| 输入例 | Gold |
|---|---|
| “以后做企业 Agent 市场研究时排除消费级产品” | `true` |
| “Tavily 本轮返回 18 条” | `false` |
| 当前 Task 的单条 Evidence | `false` |
| 只有 assistant 声称用户有某项偏好 | `false` |

长期报告：

```text
Write Gate Precision
Write Gate Recall
Write Gate F1
```

### 4.2 Cross-Task Recall

固定 Case 在同一用户的新 Task 中执行统一 `MemoryService.recall`，以 Gold relevant memory IDs 计算：

```text
Recall@5
```

### 4.3 合同场景

以下结果作为固定场景断言或 Integration Test，不形成长期优化指标：

```text
相同 write_key 重放复用既有结果
exact duplicate 复用
明确纠正产生 active / superseded
含糊冲突产生 pending_conflict
accept_new / keep_old 状态正确
forget 后停止召回并使旧 snapshot 失效
召回结果不超过 token budget
其他 user / tenant 无召回泄漏
```

跨 tenant/user 泄漏是发布硬门禁，期望值固定为零。

---

## 5. E2E Research Eval

固定研究任务完整运行：

```text
Task
  → RouteNode
  → deep: ChiefArchitect Plan
  → Web / RAG / SQL
  → MergeEvidence
  → Post-Merge Analysis Decision / Compute（按需）
  → Coverage / Deepen
  → CriticMaster
  → LeadWriter
  → Report Schema Validation / CitationValidation
  → Finalize
```

当前输入与同 Thread 近期上下文补全后的唯一查询进入 `ResearchState.query`；原始消息继续保存在 `conversation_messages`。Query Completion 的确定性合同由对应模块测试覆盖，E2E Case 只消费最终补全结果。

E2E Dataset 使用 `case_kind` 区分 Case 用途：

```text
case_kind = report_quality | route_boundary | critic_gate

report_quality
  → 进入 Factual Accuracy、Citation Coverage、Completion Rate、Faithfulness

route_boundary / critic_gate
  → 只保存逐 Case 场景断言与诊断
  → 不进入四个长期 E2E 指标的 denominator
```

Router 边界 Case 检查 direct/focused/deep 路径；Critic Gate Case 检查 Coverage 的结构判断和 Critic 的 `pass|deepen|retrieve` 语义。它们继续保留在 E2E Dataset 中，不新增 Suite 或长期报告指标。

### 5.1 长期指标

| 指标 | 类型 | 含义 |
|---|---|---|
| Factual Accuracy | LLM Judge | 报告中的原子事实是否正确 |
| Citation Coverage | Rule | 需要证据的 Claim 是否绑定可解析 Citation |
| Completion Rate | Rule | 是否成功产生可交付 Final Report |
| Faithfulness | LLM Judge | Claim 是否得到其 Citation Evidence 支持 |

四个长期指标只聚合 `case_kind=report_quality` 的 Case。

### 5.2 Factual Accuracy

每个 Case 提供版本化 Gold Facts：

```yaml
gold_facts:
  - fact_id: market_size_2025
    statement: "2025 年目标市场规模为……"
    acceptable_variants: []
    evidence_refs: []
```

Factual Judge 输出：

```yaml
claim_id: claim_017
verdict: correct | incorrect | not_verifiable
matched_gold_fact_ids: []
reason: "..."
```

```text
Factual Accuracy
  = correct Claims
    / (correct + incorrect + not_verifiable Claims)
```

未被报告覆盖的 Gold Facts 进入逐 Case 诊断，帮助识别遗漏内容。

### 5.3 Citation Coverage

Citation Coverage 由程序基于 ReportAst 与 CitationValidator 计算：

```text
Citation Coverage
  = 至少绑定一个 Citation 且 CitationValidator 可解析的 ClaimSegment
    / 全部需要证据支持的 ClaimSegment
```

该指标只判断引用是否存在且可解析；语义支持由 Faithfulness 判断。

### 5.4 Completion Rate

```text
Case completed =
  task_status == completed
  AND report.final 存在
  AND ReportAst schema valid
  AND CitationValidator.valid

Completion Rate
  = completed report_quality Cases
    / 全部 report_quality Cases
```

`report_quality` Case 的 Provider 或基础设施错误仍计入未完成 Case，同时在诊断中按错误类别汇总。`route_boundary / critic_gate` 不进入 Completion Rate denominator。

### 5.5 Faithfulness

Faithfulness Judge 读取 Claim 及其 Citation 解析得到的 Evidence：

```yaml
claim_id: claim_017
supported: true
supporting_citation_ids:
  - cit_003
reason: "Evidence 明确支持该 Claim"
```

```text
Faithfulness
  = 至少被一项 Citation Evidence 支持的 Claim
    / 全部需要证据支持的 Claim
```

未提供支持的 Citation 可由 `claim.citation_ids - supporting_citation_ids` 得到。

---

## 6. LLM Judge

Factual Accuracy 与 Faithfulness 使用两个独立 Judge 合同：

```text
Factual Judge
  输入：Research Query + Gold Facts + ReportAst ClaimSegments
  输出：每个 Claim 的 correct | incorrect | not_verifiable

Faithfulness Judge
  输入：ClaimSegment + resolved Citation Evidence
  输出：supported + supporting_citation_ids + reason
```

运行配置：

```text
固定 Judge Model Binding
固定 Prompt version
固定 Rubric version
temperature = 0
结构化 JSON 输出
按 Section 或有界 Claim batch 执行
先计算 Case 指标，再对 Case 做宏平均
```

Judge 输入不包含被评模型、Provider 或配置组名称。`config_snapshot` 保存 Judge Binding、Prompt 与 Rubric 版本。

Dataset 保留一组人工标注 Case，用于检查 Judge 与人工 verdict 的一致性。Judge 输出非法时执行有限重试；最终仍失败时固定执行：

```text
eval_case_results.outcome = judge_error
受影响的 Judge 指标 = null
  → 不计为 correct / supported
  → 不进入正常 Judge 指标的宏平均 denominator

metrics_summary.Judge Error Count += 1
EvalRun 不具备质量门禁或发布比较资格
```

诊断页面可以展示其余成功 Case 的临时指标，但该 Run 不得与无 `judge_error` 的正常 Run 做发布比较，也不得通过质量门禁。

---

## 7. Skill Eval

Skill Eval 使用同一批 Case 分别判断“该不该触发”和“触发后是否有效”。Dataset 明确给出输入、可用 Skill、期望激活集合与可判定结果：

```yaml
case_id: skill_visualization_017
input: "比较近六个月各地区销售趋势并画图"
available_skills: [data-analysis, visualization]
expected_skills: [data-analysis, visualization]
result_assertions:
  - chart_exists
  - cited_data_matches_chart
```

### 7.1 Trigger

```text
Skill Trigger Precision / Recall / F1
  → 比较实际 activated_skills 与 expected_skills
```

同时包含无需 Skill、多个 Skill 都合理和易混淆 Skill 的 Case。触发指标只判断选择，不把 Provider 或 Tool 执行失败算作触发错误。

### 7.2 Effect

对需要 Skill 的固定 Case，在相同 Model Binding、Tool Fixture、预算和随机种子下执行：

```text
skill enabled  → task success / ReAct steps / token / cost / final quality
skill disabled → task success / ReAct steps / token / cost / final quality
```

`result_assertions` 优先由规则评分；只有开放式产物才使用冻结 LLM Judge。长期报告 `Effect Success Rate`，表示启用 Skill 后满足结果断言且不劣于 disabled 对照的 Case 比例；步骤、Token、Cost 与最终质量差异作为诊断，不分别扩展成长期门禁。

---

## 8. EvalRun 与 Case Result

```text
eval_runs
  eval_run_id
  suite                    # rag | research_ltm | e2e | skill
  dataset_ref              # 不可变 Dataset Snapshot
  config_snapshot          # 当前 Suite 已解析的完整配置与 Model Bindings
  code_revision
  eval_scope_id
  status                   # running | completed | failed
  metrics_summary?
  created_at
  completed_at?
```

```text
eval_case_results
  eval_run_id
  case_id
  outcome
  metrics
  diagnostics
```

`config_snapshot` 只保存当前 Suite 实际使用的检索、Memory、模型和 Judge 配置。逐 Case 结果直接关联 `eval_run_id + case_id`，支持从汇总指标定位失败样本。

---

## 9. 长期维护的指标与门禁

```text
RAG
└─ Recall@5

Research LTM
├─ Write Gate Precision / Recall / F1
└─ Recall@5

E2E Research
├─ Factual Accuracy
├─ Citation Coverage
├─ Completion Rate
└─ Faithfulness

Skill
├─ Trigger Precision / Recall / F1
└─ Effect Success Rate
```

硬门禁与诊断：

```text
Cross-tenant/user Leakage = 0
CitationValidator.valid
Judge Error Count
Infrastructure Failure Count
幂等、冲突、遗忘与生命周期场景断言
```

C-MTEB 是 RAG Suite 的可选 Dataset，不形成额外指标或独立 Suite。

---

## 10. 实现顺序

1. 定义不可变 Dataset、稳定 Case ID、Gold 与 `eval_case_results`。
2. 实现 Suite-specific Preflight、`eval_scope` 与 Resource Manifest。
3. 接入生产 Document/Retrieval、MemoryService 与 Research Graph。
4. 实现 RAG 的 Recall@5 和 LTM 的 Write Gate/Recall 指标。
5. 实现 Citation Coverage、Completion Rate 与两类 Judge。
6. 实现 Skill Trigger 与 enabled/disabled Effect 对照。
7. 持久化 EvalRun、Case Result、配置快照和汇总指标。
8. 接入业务 Gold Dataset，并按需运行 C-MTEB 子集。

---

## 11. 验收标准

```text
□ 长期 Suite 只有 rag / research_ltm / e2e / skill
□ Dataset、config_snapshot、code_revision 与 Judge 版本可复现一次运行
□ Preflight 按 Suite 检查依赖，Fixture 通过生产 Service 写入
□ Setup 记录 Resource Manifest，Teardown 在 finally 中按实际资源执行
□ Summary 可下钻到稳定 case_id 的输入、Gold、输出与诊断
□ RAG Recall@5 按 Chunk Gold 逐 Case 计算并做宏平均；relevant_doc_ids 不进入公式；C-MTEB 只作为可选外部 Dataset
□ Research LTM 长期指标只有 Write Gate P/R/F1 与 Recall@5
□ LTM 幂等、冲突、遗忘、预算和 scope 隔离通过场景断言验证
□ E2E 长期指标为 Factual Accuracy、Citation Coverage、Completion Rate、Faithfulness，且只聚合 report_quality Case
□ Citation Coverage 与 Completion Rate 由程序计算
□ Factual Accuracy 与 Faithfulness 使用冻结 Judge 合同和结构化输出
□ judge_error 的 Case 指标为 null 且不进入正常宏平均；包含 judge_error 的 EvalRun 不具备质量门禁资格
□ Skill Trigger 使用 P/R/F1，Effect 使用相同条件下 enabled/disabled 对照
□ E2E 场景断言覆盖 RouteNode 三档路径、Coverage 结构检查和 Writer 前 Critic Gate
□ EvalRun 保存汇总，eval_case_results 保存逐 Case 结果
```
