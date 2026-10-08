# search_chunks

> 公共调用链见 [tool.md](../../tool.md)，Hybrid Retrieval 见 [rag.md](../../rag.md)。

## 1. 定位

`search_chunks` 是 Document Local RAG 的薄接口，返回与查询相关的知识库切片。

## 2. 输入

```text
query: string
filters?:
  doc_ids?: string[]
  tags?: string[]
top_k?: int = 10
```

`top_k` 服务端上限为 20。DocumentService 将候选限制在当前 tenant，并与显式 `doc_ids` 取交集。

## 3. 输出

```text
chunks[]:
  doc_id
  chunk_id
  content_hash
  text
  score?
  section_title?
  page_start?
  page_end?
truncated
degraded_channels[]?
```

`doc_id + chunk_id + content_hash` 是交给 `research_rounds`、`evidence_pool` 和 Citation 的 Document 引用。

## 4. 主流程

```text
query + 当前 tenant 的 Document 范围
  → BM25/IK + dense_vector kNN
  → RRF
  → Rerank
  → Parent Expansion
  → ToolResult
```

Graph Node 将结果写入 `research_rounds[depth].rag`；MergeEvidence 为每个采纳的 chunk 生成绑定当前 hypothesis_id 的独立 RAG Evidence。

## 5. 验收

```text
□ 只检索有效授权范围内的 ready Document
□ 返回 doc_id + chunk_id + content_hash
□ 空结果、top_k 截断和单路降级可验证
□ 与 search_documents、read_document 职责分离
```
