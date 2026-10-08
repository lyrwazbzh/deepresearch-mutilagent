# Tool Decision Flywheel

## 0. 目标与边界

数据飞轮只优化一个决策：

> DeepScout、DataAnalyst 与 CodeWizard 在 bounded ReAct 中，怎样根据当前任务、可用 Tool 和已有 Observation，选择下一次 Tool Call、生成参数，或者以 `NO_TOOL` 结束工具调用。

```text
生产
Bounded ReAct
  → Tool Decision
  → ToolDecisionTrace
  → Execution / Outcome

离线
ToolDecisionTrace
  → Eligibility / Governance
  → Label Builder
  → Sample Builder
  → Immutable Dataset Snapshot

训练与验证
Dataset
  → QLoRA / SFT
  → Offline Tool Eval
  → Safety Gate
  → Shadow
  → Canary

上线
新 Tool Model Binding
  → Agent / Graph / Tool / ResearchState 合同保持不变
  → 继续产生 ToolDecisionTrace
```

RouteNode 继续使用 Structured Output 与固定 Case Evaluation，不进入本数据飞轮。

---

## 1. 采集范围

Tool Decision 指模型面对当前允许的候选 Tool 集合，自主选择 `Tool | NO_TOOL` 及参数的决策。只采集：

```text
DeepScout bounded ReAct
DataAnalyst bounded ReAct
CodeWizard bounded ReAct

LLM → tool_call / NO_TOOL → observation → LLM → ...
```

CitationValidation、Workspace 文件发布、Checkpoint、Outbox、索引发布等由 Graph 或 Runtime 确定执行的操作不产生 ToolDecisionTrace。

`NO_TOOL` 表示当前决策位置已经不需要继续调用 Tool，Agent 应生成结果或结束当前执行；它不表示 Tool 失败后的 fallback。

---

## 2. ToolDecisionTrace

### 2.1 权威字段

```text
ToolDecisionTrace
  decision_id
  tenant_id, user_id, task_id, thread_id
  node_run_id
  decision_index
  agent_name                    # deep_scout | data_analyst | code_wizard
  model_binding                 # 本次固定 binding + revision

  context
    current_intent
    toolset_snapshot_ref        # 复用 Task capability_snapshot
    available_tools[]
      name
      schema_hash
    prior_tool_results[]        # 模型当时看到的有界摘要或稳定引用

  decision
    selected_tool | NO_TOOL
    arguments?

  execution
    status
    normalized_error?
    retry_count
    is_fallback

  outcome
    result_accepted?
    evidence_created?

  created_at

UNIQUE(tenant_id, task_id, node_run_id, decision_index)
```

`node_run_id + decision_index` 标识同一语义节点执行中的决策位置。retry 或 checkpoint replay 命中既有记录；新的语义节点执行使用新的 `node_run_id`。

### 2.2 Context 一致性

`context` 保存模型做出该决策时实际看到的有界投影。Sample Builder 直接使用该投影，不在离线阶段重新摘要或重建一份更完整的上下文。

```text
current_intent
  → 当前 Agent 实际收到的任务目标

toolset_snapshot_ref
  → 引用 Harness 在 Task 创建时冻结的 capability_snapshot

available_tools[]
  → 本次真正暴露给模型的 Tool name + schema_hash

prior_tool_results[]
  → 当时模型可见的摘要或稳定引用
```

Trace 不复制完整 Tool Schema、ResearchState、Document、Workspace 文件、Evidence 正文或 Tool 长结果，也不建立第二套 Flywheel Tool Snapshot。

### 2.3 Decision、Execution 与 Outcome

```text
decision
  = 模型选择了什么

execution
  = Tool 本身执行得怎么样

outcome
  = 该决策是否对后续任务形成有效结果
```

Tool 执行成功不自动证明选择正确，Provider 超时也不自动证明选择错误。`execution/outcome` 是 Label Builder 的参考信号，不是 SFT Sample 的模型输出。

---

## 3. 写入与治理

```text
模型产生原生 Tool Call 或 NO_TOOL
  → 使用 node_run_id + decision_index 标识决策位置
  → ToolDecisionTrace 独立于 NodeRun / ResearchState 业务事务持久化
  → retry / checkpoint replay 通过唯一键幂等复用
  → Tool 执行后更新 execution
  → 下游接受、MergeEvidence 等结果异步补 outcome
```

ToolDecisionTrace 是训练数据候选，不是 ResearchState、OTel Span、Event 或 Audit。采集失败不回滚 ResearchTask、ResearchState 或 NodeRun 的业务提交；具体可靠写入机制由实现结合现有 Outbox 或旁路 Writer 决定，本合同不新增固定通道。缺少执行或结果信息的记录可以进入人工审阅，但不自动进入训练集。

进入离线 Builder 前必须满足：

```text
用户与租户允许训练用途
输入通过 Secret、Credential、PII 与长正文清理
Tool Decision 和 arguments 可通过当时的 Tool schema 校验
toolset_snapshot_ref 与 Model Binding 可恢复
没有未解决的权限拒绝或数据污染标记
```

---

## 4. Label Builder

### 4.1 Label Contract

```text
acceptable_tools[]             # 可包含 NO_TOOL；允许多个合理选择
argument_assertions?           # schema、枚举、范围或确定性谓词
label_source
confidence                     # high | medium | low
difficulty                     # trivial | confusable | compositional
```

`acceptable_tools[]` 表达合理选择集合，不强制把存在多条正确路径的问题压成单一 `gold_tool`。`argument_assertions` 验证参数语义与约束，不要求自由文本 Query 与某份 Gold 字符串逐字相同。

### 4.2 Label 来源

```text
human_gold
curated_eval
deterministic_rule
validated_outcome
```

`validated_outcome` 可以进入训练集，但不能单独作为正式 Eval 的 Gold。Tool 执行成功、HTTP 200 或生成 Evidence 都只是弱信号。

正式 Eval 的人工/策展 Case 不进入训练集。

---

## 5. Sample Builder

训练样本使用模型原生 Tool Call 表示：

```text
input
  agent role
  current_intent
  当时可见的 Tool schemas
  当时可见的 prior Tool results

output
  tool_call(name, arguments)
  或 NO_TOOL
```

Builder 通过 `toolset_snapshot_ref + available_tools[].schema_hash` 恢复当时的 Tool Schema，并保持 Trace 中的 Context 投影不变。临时签名 URL、Secret、Credential、tenant/user/task 标识、Workspace 物理引用和原始 Provider 错误正文在生成样本前清理。

---

## 6. Dataset Snapshot

```text
dataset/{dataset_id}/{version}/
  train.jsonl
  val.jsonl
  test.jsonl
  manifest.json
```

```text
manifest
  dataset_id
  version
  created_at
  source_time_range
  sample_count
  agent_distribution
  tool_distribution
  difficulty_distribution
  git_commit
  builder_version
```

同一 `thread_id` 的全部 Tool Decision 进入同一个 split，避免同一研究链中的连续 Tool Call 泄漏到训练集和测试集两侧。Snapshot 发布后不可变；新数据或 Builder 规则产生新的 version。

---

## 7. 训练与离线评测

P0 先用 ToolDecisionTrace 做 Prompt、Model Binding 和 Sample Builder 的离线比较；数据量和标签质量达到门槛后再执行 QLoRA/SFT。训练不改变线上原生 Tool Call / `NO_TOOL` 协议。

核心指标：

```text
Tool Selection Accuracy
  = selected_tool ∈ acceptable_tools

Argument Assertion Pass Rate
  = arguments 满足 schema 与 argument_assertions 的比例

Tool Decision One-shot Success
  = Tool 选择正确
  + 参数通过 assertion/schema
  + 无二次修参

Execution Success
  = 外部 Tool 实际执行成功的比例
```

前三项评价模型决策；Execution Success 评价运行环境与 Provider 可用性。timeout、429、5xx、circuit_open 等执行错误不混入 Tool Selection Accuracy。

长期同时报告总体指标，以及按 Agent、Tool 和 difficulty 的切片。

---

## 8. Offline Gate、Shadow 与 Canary

### 8.1 Offline Gate

```text
Tool Selection Accuracy          ≥ baseline + Δ
Argument Assertion Pass Rate     不低于 baseline
Tool Decision One-shot Success   ≥ baseline + Δ

Safety Gate
  permission_violation = 0 新增
  critical tool regression 不超过约定阈值
```

Execution Success 作为环境诊断展示，不直接替代 Tool Decision 的模型门禁。

### 8.2 Shadow

```text
旧 Tool Model Binding
  → 真实执行 Tool

候选 Tool Model Binding
  → 接收相同 Context
  → 只生成 Tool / NO_TOOL 与 arguments
  → 不执行 Tool
```

Shadow 比较 Gold/Label 一致性、与旧模型的 disagreement、schema 合法率和关键安全切片。

### 8.3 Canary

离线 Gate 和 Shadow 通过后，Canary Policy / ModelResolver 在 **Task 创建并冻结 Model Binding 之前**，按 eligible Task/traffic 比例为整项 Task 分配旧或候选 Tool Model Binding：

```text
Canary Policy / ModelResolver
  → old binding | candidate binding
  → Task 创建时冻结
  → Task 执行、retry、recovery 始终使用该 frozen binding
```

Canary 扩大的是候选 binding 接管的 Task / eligible traffic 比例，不在同一 Task 的单次 Tool Call 之间动态切换。回滚只让后续新 Task 恢复旧 binding；已经运行的 Task 继续使用自身冻结的 binding。回滚不改变 Agent、Graph、Tool 或 ResearchState 合同。

---

## 9. 与其他系统的边界

| 系统 | 负责内容 |
|---|---|
| ResearchState / NodeRun | 业务状态、恢复与执行幂等 |
| capability_snapshot | Task 创建时冻结的 Tool 候选与 schema identity |
| ToolDecisionTrace | 模型自主 Tool/NO_TOOL 决策的训练候选 |
| OTel Trace | 延迟、错误、Provider 调用链与成本 |
| Evaluation | 固定人工/策展 Dataset 上的正式质量结果 |
| Audit | 敏感操作与授权决策 |

数据飞轮只替换三个执行 Agent 使用的 Tool Model Binding。RouteNode、ChiefArchitect、Graph 控制逻辑和 ToolRuntime 不从该 Dataset 训练。

---

## 10. P0 实现顺序

1. 固定 ToolDecisionTrace schema、决策身份、幂等键和脱敏规则。
2. 接入 DeepScout、DataAnalyst、CodeWizard 的 bounded ReAct 决策位置。
3. 复用 Task capability_snapshot 恢复可用 Tool 集合与 schema。
4. 实现 Label Builder、人工审阅与 Sample Builder。
5. 发布不可变 Dataset Snapshot，并执行 thread-level split。
6. 建立 Tool Selection、Argument Assertion、One-shot 与 Safety Gate。
7. 本阶段完成 QLoRA/SFT、Shadow 与 Task binding freeze 前 Canary 分流的代码路径和自动化测试；数据量与标签质量达到门槛后，再真实执行训练、Shadow 和生产 Canary rollout。

---

## 11. 验收标准

```text
□ 只有 DeepScout、DataAnalyst、CodeWizard 的模型自主决策产生 ToolDecisionTrace
□ Graph/Runtime 的确定性调用不进入 Tool Decision Dataset
□ NO_TOOL 表示结束当前 Tool 调用过程，不表示执行 fallback
□ Trace Context 与模型当时真实输入一致，离线阶段不重新摘要
□ Tool 候选与 schema 复用 capability_snapshot，不建立第二套快照
□ Tool 执行成功不自动充当决策 Gold
□ 正式 Eval Gold 不进入训练集
□ 同一 Thread 的样本只进入一个 split
□ Dataset Manifest 可按 Agent、Tool、difficulty 与 builder version 还原分布和构建规则
□ Tool Selection、Tool Decision One-shot 与 Safety Gate 分开计算
□ ToolDecisionTrace 独立于业务事务持久化；采集失败不回滚 ResearchTask / ResearchState / NodeRun 提交
□ Canary 在 Task binding freeze 前分流；Task 内及 retry/recovery 不切换 binding
□ QLoRA/SFT、Shadow、Canary 的代码路径与自动化测试完成；数据未达到门槛时不要求真实训练或生产 rollout
□ 新模型只替换 Tool Model Binding，不改变 Agent、Graph、Tool 或 ResearchState 合同
```
