# DeepResearch-mutilagent：Document System

## 0. 定位

Document System 管理知识库原始文件，并为 Local RAG 建立可检索索引：

```text
知识库文件上传
  → 原始文件存储
  → 文本解析 / OCR
  → Chunk
  → Embedding
  → Elasticsearch Index
  → Local RAG
```

Document 是知识库来源。RAG 命中的 Chunk 经 Graph 采纳和 MergeEvidence 后，才成为当前 ResearchTask 的 evidence。

“知识库”是产品界面名称；领域实体、API 与引用协议统一使用 `Document`。Elasticsearch 的 `document_chunks` 是 Document 的可重建检索索引。

P0 支持：

```text
PDF
DOCX
TXT
MD
HTML
```

---

## 1. 数据合同

### 1.1 documents

```text
documents
  doc_id
  tenant_id
  name
  media_type
  tags[]?

  storage_ref
  content_hash
  size_bytes

  status: pending | ready | failed
  published_generation?       # 当前对 RAG 可见的索引 generation
  indexed_at?
  deleted_at?                 # 逻辑删除与 retention 起点；不扩展 status 枚举

  created_at
  updated_at
```

- `name` 是用户可见的原始文件名。
- `media_type` 由服务端根据文件字节识别，用于 Parser 选择、上传校验和原文件下载响应。
- `storage_ref` 是 DocumentRepository 内部使用的对象存储物理引用，指向原始文件。它不进入 ResearchState、ToolResult、Evidence、Citation 或对外 API，也不保存临时签名 URL。
- `content_hash` 是原始文件字节的 SHA-256，用于完整性校验和去重。
- `tags` 用于知识库检索过滤。

### 1.2 document_chunks

```text
document_chunks
  chunk_id
  doc_id
  generation                  # Document 内部索引 generation
  parent_id?
  ordinal
  section_title?
  page_start?
  page_end?
  chunk_hash
  metadata?
```

PostgreSQL 保存 Chunk identity、层级和引用元数据。MinIO 保存 Document 原始文件。Elasticsearch 保存可重建的 Chunk 正文、检索字段和向量：

```text
Elasticsearch document_chunks index
  chunk_id
  doc_id
  generation
  parent_id?
  chunk_level: parent | child
  text
  embedding                  # dense_vector；Chunk 向量的唯一检索副本
  content_hash
  chunk_hash
  section_title?
  page_start?
  page_end?
  tenant_id
  tags[]?
```

ModelRuntime 为 Chunk 生成向量，Indexer 将 Chunk 文本与向量写入 Elasticsearch。Elasticsearch 负责保存 Chunk text、向量并执行检索；PostgreSQL 只保存 Document 元数据、Chunk identity、层级与引用定位，不复制 Chunk text 或 embedding，也不引入独立 Vector DB。

`chunk_id` 是具体切片的不可变唯一标识。重新切分产生新的 Chunk；同一 `chunk_id` 不指向不同内容。

知识库引用统一使用：

```text
doc_id
chunk_id
content_hash
```

页码与章节名称作为展示和定位辅助信息，不参与引用身份。

## 2. 状态与重建

Document 状态只表达当前是否存在可供检索的已发布索引：

```text
pending → ready | failed
```

每个 Document 使用一条 `document_ingestions` 当前处理控制记录：

```text
document_ingestions
  tenant_id
  doc_id
  generation
  status                 # queued | processing | succeeded | failed
  stage                  # parsing | chunking | embedding | indexing | publishing
  attempt_count
  error_code?
  updated_at
```

首次处理使用 `generation=1`；retry 复用 generation 并增加 attempt；rebuild 递增 generation。错误详情进入 Trace/日志，API 根据 `error_code` 映射安全消息与重试策略。

```text
content_hash = SHA-256(原始文件字节)
chunk_hash   = SHA-256(切片正文)
```

重建索引时，从原始文件重新解析并生成新的 Chunk 集合。新 generation 完整写入并 refresh Elasticsearch 后，PostgreSQL 通过 CAS 更新 `published_generation`；RetrievalGateway 只召回该 generation，实现检索可见性的原子切换。切分结果发生变化时生成新的 `chunk_id`。

`documents.published_generation` 是唯一的 RAG generation 可见性指针：`document_ingestions.generation` 只表示当前正在处理哪一代。系统不再增加 `active_generation / current_index_version / visible_generation` 等第二套发布状态。

---

## 3. Knowledge Document Ingestion

上传入口接收一个知识库文件流：

```text
DOCUMENT_UPLOAD_MAX_BYTES = 100 MiB
TENANT_DOCUMENT_BYTES_MAX = 50 GiB
```

主流程：

```text
上传 PDF / DOCX / TXT / MD / HTML
  → tenant membership、操作权限与配额校验
  → 创建 document_upload_reservations(tenant_id,idempotency_key,doc_id)
  → 流式写入 documents Bucket 临时对象
       同时计算 size_bytes、content_hash、media_type
  → 校验通过后发布为 Document 原始对象
  → PostgreSQL 事务：
       创建 Document(status=pending, storage_ref, hash, size, media_type)
       写 reservation.content_hash
       初始化 document_ingestions(generation=1,status=queued,attempt_count=0)
       写 outbox_messages(kind=document.ingest,payload=doc_id)
  → outbox-dispatcher 发布 ingestion Job(doc_id)
  → Worker CAS：queued → processing，同时 attempt_count += 1
  → Parser / OCR
  → Normalize in memory
  → Parent / Child Chunk
  → ModelRuntime 生成 Chunk 向量
  → Elasticsearch 按 generation 写 Chunk text、检索字段与向量并 refresh
  → PostgreSQL 写 document_chunks identity 与引用元数据
  → CAS 更新 documents.published_generation
  → Ingestion.status=succeeded，Document.status=ready
```

Worker 的阶段更新与发布均校验当前 `generation + attempt_count + status=processing`。已有可用索引时，rebuild 期间和失败后 Document 都保持 `ready`，继续使用旧 `published_generation`。对象存储中的原始文件是索引重建来源。

---

## 4. Parser 与 Chunk

P0 配置：

```text
PARENT_CHUNK_MAX_TOKENS=1200
CHILD_CHUNK_MAX_TOKENS=400
CHILD_CHUNK_OVERLAP_TOKENS=80

EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024
DOCUMENT_INDEX_SCHEMA=document_chunks_v1
```

切分流程：

```text
Parser 提取标题、章节、段落、列表和表格文本
  → 语义结构形成 Parent
  → 超长 Parent 按段落与句子继续切分
  → Parent 内生成 Child chunks
  → 每个 Chunk 分配 chunk_id、ordinal、chunk_hash 和可用的页码/章节元数据
```

PDF 使用 Parser/OCR 提供的页码作为可选 locator；DOCX、TXT、MD 和 HTML 使用可选章节名称。Local RAG 的稳定定位单位是 `chunk_id`。

---

## 5. Local RAG 对接

```text
search_documents
  → 按 name / tags 发现当前用户可读取的知识库文件

search_chunks
  → 在当前可查询索引执行 BM25/IK + dense_vector kNN
  → RRF 融合候选 → Rerank → Parent Expansion
  → 返回 chunk text + doc_id + chunk_id + content_hash

read_document
  → 按 doc_id + chunk_id 读取已索引 Chunk
```

```text
Local RAG result
  → research_rounds[depth].rag
  → MergeEvidence
  → evidence_pool
```

RAG 查询算法见 [`rag.md`](rag.md)，Document Tool 合同见 [`tools/document/`](tools/document/)。

---

## 6. 服务职责

```text
DocumentService
  create / finalize_upload / get / list / resolve / delete

IngestionService
  parse / normalize / chunk / embed / index / rebuild

RetrievalGateway
  tenant filter / BM25 / kNN / RRF / rerank / parent expansion
```

DocumentService 管理知识库文件与元数据；IngestionService 通过统一模型接口生成 embedding 并管理索引构建；RetrievalGateway 为 Local RAG 提供查询。

---

## 7. 权限与删除

Document 是 tenant 级知识库资源，不按上传用户划分。Document API、Ingestion Worker 和 Local RAG 统一通过 DocumentService 校验 `tenant_id`；HTTP 操作者还必须是当前 tenant 的有效成员并具有对应读写权限。上传者进入 Audit，不成为 Document owner。RAG Tool 的 `doc_ids / tags` 只能缩小当前 tenant 的候选范围。

删除流程：

```text
用户删除 Document
  → 设置 documents.deleted_at，立即从知识库列表隐藏
  → DocumentService 将其排除出新的 Local RAG 候选范围
  → retention 窗口内保留原始对象、document_chunks metadata 与已发布 ES Chunks
  → 历史 Evidence 仍可按 doc_id + chunk_id + content_hash 受控解析来源正文
  → retention 到期后异步删除 ES Chunks、document_chunks metadata 与原始对象
  → 删除 documents 实体或只保留不含正文的删除审计 / tombstone
```

`deleted_at` 表达 Document 资源删除生命周期，不是另一套索引 generation 状态，也不改变 `status=pending|ready|failed` 的语义。RetrievalGateway 的候选范围始终排除 `deleted_at IS NOT NULL`；retention 内的精确读取只服务已有 Evidence provenance，不允许重新进入搜索候选。物理清理完成后，历史 Evidence 仍保留 locator 与 `content_hash`，但来源正文不再可访问。

---

## 8. 可观测与错误

```text
doc_id
generation
attempt_count
stage
media_type
content_hash
status
chunk_count
ingestion_duration
parse_failure
index_failure
ready_lag
error_code
```

错误分类：

```text
unsupported_media
upload_too_large
parse_failed
embedding_failed
index_failed
policy_denied
not_found
```

---

## 9. 测试

```text
□ PDF / DOCX / TXT / MD / HTML 的首次 Ingestion 完成 queued → processing → succeeded，Document 完成 pending → ready
□ media_type 由文件字节识别并选择正确 Parser
□ 用户可见文件名统一由 name 表示
□ Ingestion 的 generation、stage、attempt_count 和 error_code 只写入 document_ingestions
□ retry 复用 generation，rebuild 递增 generation，迟到 Worker 无法提交
□ rebuild 期间与失败后继续使用旧 published_generation
□ published_generation 是唯一 RAG generation 可见性指针，不存在第二套 active/current/visible generation
□ 重新切分生成新的 chunk_id，同一 chunk_id 不指向不同内容
□ document_chunks 使用 chunk_id 和页码/章节元数据定位
□ Elasticsearch 清空后可从原始文件重建索引
□ tenant_id 在 Document API、BM25 与 kNN 召回前使用同一访问范围
□ Chunk embedding 只保存在 Elasticsearch dense_vector 字段
□ PostgreSQL 不复制 Chunk text/embedding，MinIO 保存原文件，不引入独立 Vector DB
□ Document 删除立即停止新检索，retention 内历史 Evidence 可解析，期满后异步物理清理
```

---

## 10. 验收标准

```text
□ Document System 只管理知识库原始文件、Chunk identity 与 Local RAG 索引
□ documents 使用 name、media_type、storage_ref、content_hash、status 与 published_generation
□ Document 状态收敛为 pending / ready / failed
□ Parser 执行状态进入 document_ingestions，详细异常进入 Trace / 日志
□ Document 引用统一使用 doc_id / chunk_id / content_hash
□ Elasticsearch 保存 Chunk text 与 embedding，原始文件支持索引重建
□ deleted_at 只控制 Document 删除生命周期，不形成第二套 generation 可见性状态
```

---

## 11. 实现顺序

```text
1. documents / document_ingestions / document_chunks DDL 与 tenant scope
2. DocumentService + 流式知识库文件上传 + 原始对象存储
3. Ingestion Worker：Parser / OCR → Normalize → Parent/Child Chunk
4. ModelRuntime Embedding + Elasticsearch `document_chunks` mapping/index
5. BM25/IK + kNN + RRF + Rerank + Parent Expansion
6. generation 可见性切换、重建、删除与权限测试
7. RetrievalGateway 接入 Local RAG
```
