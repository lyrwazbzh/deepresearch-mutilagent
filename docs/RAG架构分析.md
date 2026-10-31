# 搜索 Agent 与 RAG 架构分析

> 调研日期：2026-07-14
> 范围：`backend/app/service/` 下的检索、入库、文档处理、搜索 agent 相关代码

## 一、核心结论（先回答你的问题）

### 1. 本地搜索是不是 RAG？
是。本地搜索是标准 RAG 的**检索（Retrieval）环节**：把用户问题向量化，到 Milvus 里做向量近邻搜索，返回相关 chunk。整个项目"本地搜索 + 网络搜索 + LLM 生成"的组合，就是典型的 RAG + Web Search 混合架构。

### 2. 是否需要开发者离线做"文档切块 / 向量化 / 入库"？
是，离线预置走的就是这条流水线，入口在 `docmind_service.py::process_document_with_docmind`：

```
原始文档 → DocMind 解析(阿里云) → chunk_text 切块 → generate_embedding 向量化 → Milvus 入库
```

- 解析：阿里云 DocMind（`docmind_api20220711`），输出 markdown 文本
- 切块：`chunk_text(text, chunk_size=500, overlap=50)`，按句子边界（。！？.\n）切
- 向量化：阿里 DashScope `text-embedding-v4`，1024 维
- 入库：`milvus.insert_documents(collection_name, documents)`

### 3. 在线检索是不是去 Milvus 找相关 chunk？
是。`retrieval_service.py::retrieve_content`：

```
question → generate_embedding([question]) → milvus.search(collection, query_vector, top_k) → 返回 chunks
```

Milvus 索引参数：`IVF_FLAT` + `COSINE` 距离，`nlist=128`，查询 `nprobe=10`。
返回字段：`id / doc_id / kb_id / filename / content / chunk_index / score`。

### 4. 用户上传文件是在线入库的吗？入到哪个库？
是**在线入库**，但实际处理是**后台异步**的。入口 `knowledge_router.py::POST /knowledge-bases/{kb_id}/documents`：

1. 接收文件 → 存到 `/tmp/knowledge_uploads/`
2. DB 建 Document 记录（status=`pending`）
3. `BackgroundTasks` 投递 `process_document` → 调 `process_document_with_docmind`
4. 走和离线**完全相同**的流水线（DocMind→切块→向量化→Milvus）
5. 完成 status=`completed`，写回 `chunk_count`

**入到哪个库：** Milvus，collection 名 = `kb_{知识库名}.lower().replace(" ", "_")`。**每个知识库对应一个独立的 Milvus collection**。

---

## 二、⚠️ 发现的关键问题：collection 命名不一致（会导致用户文档搜不到）

这是最值得修的点：

| 环节 | collection 名 | 位置 |
|------|--------------|------|
| 用户上传入库 | `kb_{知识库名}` | `knowledge_router.py:93` |
| 离线预置入库 | `kb_{知识库名}` | `retrieval_service.py:91`（retrieve_from_knowledge_base）|
| **Scout agent 本地搜索** | **`"knowledge_base"`（硬编码）** | `scout.py:1031` |

`scout.py::_execute_local_search` 里写死了 `collection_name="knowledge_base"`，但用户上传和预置文档都入到 `kb_xxx` 集合。**两者对不上**，意味着：

- DeepResearch 流程里 Scout 的"本地知识库搜索"基本搜不到用户上传的文档（除非有人手动往名为 `knowledge_base` 的集合灌数据，或知识库恰好叫 `knowledge_base` 且经 `kb_` 前缀转换后仍匹配——但转换后是 `kb_knowledge_base`，还是对不上）。
- `retrieval_service` 的 `retrieve_content` 是对的（用 `kb_{name}`），但它没被 Scout 调用。

**修复方向：** 让 Scout 遍历用户所有知识库的 collection（`kb_*`），或把所有文档统一入到一个公共 collection + 用 `kb_id` 字段过滤（Milvus 的 search 已支持 `expr=kb_id==...` 过滤，现成能力没用上）。

---

## 三、另一套并存的文档服务（注意区分）

项目里其实有**两套文档方案并存**，容易混淆：

- **方案 A（本地 Milvus，主力）**：`knowledge_router.py` + `docmind_service.py` + `milvus_service.py` + `retrieval_service.py`。有知识库管理、用户鉴权、后台异步处理。**用户实际用的是这套。**
- **方案 B（外部 API，疑似 RAGFlow）**：`document_router.py` + `document_service.py`。调用一个带 `base_url + api_key + dataset_id` 的外部服务（`/api/v1/datasets/...`、`/api/v1/retrieval`），有 dataset 概念。这套和本地 Milvus 没关系，看起来是早期方案或备用。

两套的 `/upload`、`/retrieve` 接口路径不同，别搞混。

---

## 四、历史遗留：注释与实现不符

多处注释写"ES 存储 / ES 索引"（`knowledge_router.py:76`、`docmind_service.py:258` 的 `index_name` 参数注释），但实际代码全用 Milvus。应该是从 Elasticsearch 迁移到 Milvus 后注释没更新，建议清理以免误导。

---

## 五、关键文件索引

| 文件 | 作用 |
|------|------|
| `service/milvus_service.py` | Milvus 连接 / 建集合 / 插入 / 向量搜索 / 删除 / 按 filename 查 chunk |
| `service/embedding_service.py` | `generate_embedding`（v4, 1024维）+ `rerank_similarity`（DashScope rerank）|
| `service/docmind_service.py` | DocMind 解析 + `chunk_text` 切块 + `process_document_with_docmind` 全流水线 |
| `service/retrieval_service.py` | `retrieve_content` / `retrieve_from_knowledge_base`（正确用 kb_xxx）|
| `router/knowledge_router.py` | 知识库 CRUD + 用户上传（后台异步入库）+ chunk 查看 |
| `router/document_router.py` | 方案 B 的外部 API 路由（RAGFlow 风格）|
| `service/deep_research_v2/agents/scout.py` | DeepResearch 的搜索 agent，`_execute_local_search` 硬编码 collection（bug）|
| `router/search_router.py` | 单独的网络搜索接口（Serper/Bocha）|

## 六、Milvus collection schema

字段：`id`(主键) / `doc_id` / `kb_id` / `filename` / `content`(65535) / `chunk_index` / `vector`(1024维)
索引：`vector` 字段 `IVF_FLAT` + `COSINE`，`nlist=128`
查询：`nprobe=10`，支持 `kb_id` 表达式过滤
