# Web Tools：search_web / read_webpage

## 0. 定位

Web 检索由两个 Tool 组成：

```text
search_web    → 搜索候选网页，返回 URL 与短摘要
read_webpage  → 读取选定网页，返回正文或 Workspace 工作记忆引用
```

DeepScout 根据当前轮 `plan.hypotheses[].queries[channel=web]` 或 Critic 的 retrieve 查询调用这两个 Tool。

## 1. search_web

### 输入

```text
query: string
source: web | paper = web
max_results?: int = 5
domains?: string[]
published_after?: date
published_before?: date
```

### 输出

```text
hits: [{
  title
  url
  canonical_url
  snippet
  source
  published_at?
}]
```

SearchHit 只是候选信息，不能直接成为 Evidence。DeepScout 结合假设、来源和摘要选择需要读取的页面。

## 2. read_webpage

### 输入

```text
url: string
page_index?: int = 0
page_size_chars?: int = 12000
```

### 输出

```text
title
source_uri
canonical_url
retrieved_at
content_hash
summary
content?                 # 当前预算内的正文
workspace_path?          # 长正文外置后的工作记忆引用
page_index
next_page_index?
has_more
```

### 主流程

```text
URL canonicalization
  → SSRFGuard 与出口策略
  → Provider 读取并抽取正文
  → 生成 title / source_uri / retrieved_at / content_hash
  → 正文适合 ToolResult 预算：完整当前页 content 写入 ToolResult.data
  → 正文超过预算或明确需要文件消费：WorkspaceService.finalize_file
  → 返回 summary + workspace_path
  → Graph Node 写入 research_rounds[depth].web
```

短结果的 `content_hash` 校验规范化后的完整 `data`；长结果的 `content_hash` 校验 Workspace 文件。`source_uri + retrieved_at` 随两类结果写入 `research_rounds[depth].web`，由 MergeEvidence 创建正式 Evidence 时用于 Web 来源追溯。

Workspace 是当前 Task 的工作记忆。外置文件由 Runtime 管理，DeepScout 将 `workspace_path` 随分支结果交给 MergeEvidence；Critic 和 Writer 只通过正式 Evidence 读取相应引用。

## 3. 假设驱动的 Web 研究

DeepScout 在有界 ReAct 中完成以下过程：

```text
hypothesis + queries
  → SemanticCache 查询
  → search_web
  → DOI / canonical URL 精确去重
  → SimHash / embedding 近重复识别
  → 来源质量初排
  → read_webpage
  → Passage 切分
  → hypothesis × passage rerank
  → support / refute / context 判断
  → research_rounds[depth].web
```

每个采用结果携带：

```text
hypothesis_id
stance                    # support | refute | context
relevance
excerpt
```

来源质量用于候选筛选，不扩展 Evidence Schema。DeepScout 不直接写入 `evidence_pool`，只把完成正文读取、定位、相关性与 stance 判断的采用结果写入 `research_rounds[depth].web`；MergeEvidence 统一生成正式 Evidence。无关候选不进入分支采用结果。不同来源即使内容近似也保留独立分支结果，并由 MergeEvidence 生成独立 Evidence；近重复识别只用于减少重复读取和模型计算。

## 4. SemanticCache

Web Search 在真实 Provider 调用前使用统一 SemanticCache：

```text
operation=web_search
constraint_hash          # domains / date range / source / provider policy
query embedding
bounded payload
expires_at
```

只有硬约束一致且 TTL 有效时才进行相似度匹配。命中后仍生成当前 Task 的正常 ToolResult，Cache 不成为 Evidence 或 Research LTM。

## 5. Provider

```text
search_web(web)    → Tavily，按运行策略使用 Jina Search fallback
search_web(paper)  → arXiv，按运行策略使用 Web fallback
read_webpage       → Jina Reader；失败或动态内容信号返回 Observation，由 DeepScout 显式调用 render_page
```

精确去重顺序：

```text
arXiv ID / DOI
  → canonical URL
  → content_hash
```

## 6. 限额与预算

```text
query                         ≤ 400 chars
max_results                   默认 5；hard limit 20
normalized snippet            每条 ≤ 2,000 chars
Search decoded response       ≤ 2 MiB
Webpage decoded response      ≤ 10 MiB
page_size_chars               默认 12,000；hard limit 40,000
Search timeout                15 s
Read timeout                  30 s
```

每次真实 Search Provider 调用在 TaskBudget 中执行 `search_calls reserve → settle`；SemanticCache 命中不增加真实搜索调用数。查询数量由 ChiefArchitect 按假设优先级和剩余预算控制。

## 7. URL 与安全处理

```text
1. 接受 http / https URL
2. scheme 与 host 规范化为小写
3. 去除默认端口、fragment 与已知 tracking 参数
4. query 参数按 key 排序
5. redirect、DNS、Host 与最终地址经过 SSRFGuard
6. canonical_url 用于候选去重与读取幂等
```

## 8. 幂等与状态

`search_web` 是只读调用。`read_webpage` 在产生 Workspace 文件时使用：

```text
operation_key = sha256(
  tenant_id | task_id | node_run_id | read_webpage:{canonical_url}:{page_index}
)
```

同一 URL 的不同正文页使用不同稳定语义槽；相同 URL、`page_index` 与 NodeRun 的逻辑读取重放时复用已发布的 Workspace 文件。内容变化通过新的 `content_hash` 表达。

## 9. 错误与恢复

| 情况 | error | retry / fallback |
|---|---|---|
| 参数或 URL 校验失败 | `invalid_argument` | 返回调用方 |
| 出口策略未通过 | `policy_denied` | 返回调用方 |
| Provider API 认证失败 | `provider_auth_error` | fallback |
| deadline | `timeout` | retry / fallback |
| 限流 | `rate_limited` | 按预算 retry / fallback |
| Provider 故障 | `provider_unavailable` | retry / fallback |
| DNS、连接、TLS、代理或连接重置 | `provider_unavailable`（内部 cause 保留具体原因） | retry / fallback |
| 响应超过上限 | `limit_exceeded` | 返回调用方 |
| 正文解析失败 | `tool_error` | 返回 Observation，由 DeepScout 决定是否显式调用 render_page |
| 用户取消 | `cancelled` | 结束本次调用 |

## 10. 可观测

```text
tool.name
query_length
requested_max_results
returned_count
dedup_count
near_dedup_count
cache_hit
page_index
returned_chars
has_more
error.type
```

Provider retry / fallback 使用 `tool.retry`、`tool.fallback` Event，实际网络请求使用 HTTP CLIENT Span。Circuit Breaker 的拒绝与状态切换使用独立 Event 和指标，不在每个 Tool Span 重复记录状态快照。查询正文、URL 与网页内容不进入常规 Span。

## 11. 验收标准

```text
□ search_web 返回规范化 URL 与有界摘要
□ SearchHit 只作为候选；DeepScout 完成正文读取和假设相关性判断后写入 research_rounds.web，正式 Evidence 只由 MergeEvidence 生成
□ DOI / URL 精确去重、SimHash / embedding 近重复识别可验证
□ 来源质量参与候选筛选；Evidence 只保留 excerpt、stance 与 relevance
□ SemanticCache 先匹配硬约束再执行相似度匹配，命中后仍生成正常 ToolResult
□ read_webpage 为短正文返回完整 data，并保留 has_more / next_page_index
□ read_webpage 为长正文返回 summary 与 workspace_path
□ Workspace 文件包含稳定 content_hash 与来源元数据
□ research_rounds 保存完整短结果或 Workspace 引用，以及 source_uri / retrieved_at
□ URL canonicalization、SSRFGuard、timeout、fallback 与幂等可验证
□ read_webpage 的稳定语义槽包含 canonical_url + page_index，同一逻辑读取重放复用同一 Workspace 文件
```
