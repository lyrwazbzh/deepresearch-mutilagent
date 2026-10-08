# DeepResearch-mutilagent：RAG / Hybrid Retrieval

## 0. 定位

```text
Document System
  → 原始文件、解析、Chunk、权限与索引构建

Elasticsearch document_chunks
  → Chunk 文本、BM25/IK 字段与 dense_vector

RetrievalGateway
  → tenant-first 双路召回、RRF、Rerank 与 Parent Expansion

DeepScout
  → search_documents / search_chunks / read_document
```

“知识库”是产品名称；RAG 的领域来源是 `Document`。命中的 Chunk 写入 `research_rounds[depth].rag`，再由 MergeEvidence 汇入 `evidence_pool`。

---

## 1. 索引与模型职责

同一个 Elasticsearch `document_chunks` 索引保存 Parent/Child Chunk 的可检索副本：

```text
doc_id
chunk_id
generation                  # Document 内部索引 generation
parent_id?
chunk_level                 # parent | child
text                        # BM25 / IK
embedding                   # dense_vector
content_hash
chunk_hash
tenant_id
tags[]
section_title?
page_start?
page_end?
```

```text
Document Ingestion
  → Parser / OCR
  → Parent / Child Chunk
  → ModelRuntime 生成向量
  → Indexer 写入 text + dense_vector
```

Chunk 向量只保存在 Elasticsearch，不复制到 PostgreSQL、MinIO 或独立向量数据库。PostgreSQL 保存 Document 与 Chunk identity/引用元数据；MinIO 保存原始文件。Elasticsearch 索引可由两者重建。

Rerank 使用统一模型适配层。具体模型可以由 Elasticsearch inference endpoint 或外部 Reranker Adapter 提供，不进入 ResearchState、Evidence 或 Citation schema。

---

## 2. RetrievalGateway

RetrievalGateway 是内部检索编排服务，不是 Agent，也不写 ResearchState 或 Workspace。

```text
query + AuthContext + doc_ids? + tags? + top_k
  → DocumentService 解析允许读取的 Document 候选
  → 解析每个 Document 的 published_generation
  → Query Normalize
  → ModelRuntime 生成 query vector
  ├─ Elasticsearch standard retriever：BM25 / IK
  └─ Elasticsearch knn retriever：dense_vector kNN
  → RRF 合并候选
  → Reranker 对候选重排
  → Child → Parent Expansion
  → RetrievalResult
```

RetrievalGateway 对上层只暴露这一份合同。P0 默认由应用层对 Elasticsearch 返回的两路排名执行 RRF；部署版本与 License capability 支持时可以切换到原生 RRF。两种实现返回相同 `RetrievalResult`，不改变 Document、ResearchState、Evidence 或 Citation。

Elasticsearch Retriever API 的能力与组合方式见官方文档：[Retrievers overview](https://www.elastic.co/docs/solutions/search/retrievers-overview)、[Ranking and reranking](https://www.elastic.co/docs/solutions/search/ranking)、[Reciprocal rank fusion](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion) 与 [Semantic reranking](https://www.elastic.co/docs/solutions/search/ranking/semantic-reranking)。

---

## 3. Tenant 范围与过滤

Agent 可见的检索过滤为：

```text
doc_ids[]?
tags[]?
```

DocumentService 按 [`auth_tenant.md`](../04_platform/auth_tenant.md) 解析当前 tenant 的可读取 Document 及其 `published_generation`；显式 `doc_ids`、`tags` 只能缩小该范围。BM25 与 kNN 在召回前应用完全相同的 tenant 与 generation filter。授权文档负责候选边界，本文件负责双路召回、融合、重排与 provenance。

---

## 4. 双路召回、RRF 与 Rerank

双路召回：

```text
BM25：IK 分词后的 query → Child Chunk Top-K
Vector：query embedding → Child Chunk kNN Top-K
```

RRF 只使用每个通道的名次，不要求对 BM25 与向量分数进行 Min-Max 归一或固定加权：

```text
rrf_score(d) = Σ 1 / (rank_constant + rank_i(d))
```

检索顺序固定为：

```text
BM25 Top-K + kNN Top-K
  → RRF candidate window
  → Rerank candidate set
  → Final Top-K
  → Parent Expansion
```

候选窗口与 Final Top-K 属 Retrieval 配置，不写入 ResearchState。Reranker 失败或超时时返回 RRF 排名并标记 `rerank_skipped`。

---

## 5. Parent / Child

```text
命中并重排 Child Chunk
  → 根据 parent_id 读取 Parent 文本
  → Parent 文本作为 Agent 上下文
  → Child 的 doc_id + chunk_id + content_hash 作为 Evidence 定位
```

Child 保留精确定位，Parent 提供完整上下文。重新切分产生新的 `chunk_id`，旧 ID 保持原内容身份，不指向新切片。

---

## 6. Tool 接口

| Tool | 职责 |
|---|---|
| `search_documents` | 按名称、标签与摘要发现可读取的 Document |
| `search_chunks` | 调用完整 Hybrid Retrieval，返回相关 Chunk |
| `read_document` | 按已知 `doc_id / chunk_id` 读取 Chunk 或展开上下文 |

`search_chunks` 保持薄接口：

```text
query + doc_ids? + tags? + top_k
  → chunks[]
       text/summary
       doc_id
       chunk_id
       content_hash
       score
```

权限范围、query embedding、双路召回、RRF、Rerank 与 Parent Expansion 均由 RetrievalGateway 完成。

---

## 7. Graph 与 Evidence

```text
search_chunks
  → RetrievalGateway
  → RetrievalResult
  → research_rounds[depth].rag
  → MergeEvidence
  → evidence_pool
```

RAG 分支只向 MergeEvidence 提交采用结果：

```text
hypothesis_id
summary / excerpt
doc_id + chunk_id + content_hash
stance
relevance
```

MergeEvidence 按 [`state.md`](../01_architecture/state.md) 的统一 Evidence Contract 生成正式 Evidence；RAG 文档不维护第二套 Evidence Schema。Document 来源始终使用 `doc_id + chunk_id + content_hash` 作为稳定 provenance。

`read_document` 只属于 DeepScout Tool Allowlist。CriticMaster 或 LeadWriter 需要核对 Evidence 原文时，由 Context Builder 根据 Evidence 中的 `doc_id / chunk_id` 读取必要片段并注入 Context Packet，不向二者开放 Tool 调用。Document 内容保持在 Document System，不复制到 Workspace。

---

## 8. 指标与评测

```text
线上：
  retrieval_latency_seconds{stage=bm25|knn|rrf|rerank|expand}
  retrieval_empty_total
  retrieval_degraded_total{stage}
  retrieval_tenant_denied_total

离线：
  Recall@5
  BM25 baseline / current production retrieval 对比
  Parent Expansion 后的回答完整性
```

---

## 9. 验收标准

```text
□ Elasticsearch document_chunks 同时保存 Chunk text 与 dense_vector
□ ModelRuntime 生成向量，Elasticsearch 保存并检索向量
□ BM25 与 kNN 在召回前使用同一 tenant_id 与 published_generation
□ 两路候选先 RRF、再 Rerank、最后 Parent Expansion
□ P0 默认应用层 RRF；原生 RRF 只作为 capability 可选实现
□ 原生与应用层 RRF 返回相同 RetrievalResult 合同
□ Evidence 固定使用 doc_id + chunk_id + content_hash
□ RAG 分支只写 research_rounds；MergeEvidence 按 state.md 的统一合同生成正式 Evidence
□ read_document 只由 DeepScout 调用；CriticMaster / LeadWriter 通过 Context Builder 接收必要原文片段
□ ES 索引可从 Document 元数据、Chunk identity 与原始文件重建
```

---

## 10. 实现顺序

1. `document_chunks` mapping：IK text、dense_vector、tenant_id、generation 与 Parent/Child 字段。
2. ModelRuntime Embedding 与批量 Indexer。
3. RetrievalGateway：tenant filter、BM25、kNN、RRF、Rerank、Parent Expansion。
4. `search_documents / search_chunks / read_document` 对接统一结果合同。
5. 跨租户检索测试与小规模标注评测集。
