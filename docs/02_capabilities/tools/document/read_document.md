# read_document

> 公共调用链见 [tool.md](../../tool.md)，Document 数据合同见 [document_system.md](../../document_system.md)。

## 1. 定位

按稳定 Document 引用读取一个已索引知识库切片：

```text
doc_id + chunk_id
  → DocumentService 读取权限校验
  → Chunk text
```

文档发现使用 `search_documents`，相关片段召回使用 `search_chunks`。

## 2. 输入

```text
doc_id: string
chunk_id: string
```

Runtime 注入 `tenant_id` 与 `user_id`。

## 3. 输出

```text
doc_id
chunk_id
content_hash
content
section_title?
page_start?
page_end?
```

`content_hash` 对应 Document 原始文件；`chunk_id` 唯一标识本次读取的具体切片。

## 4. 主流程

```text
doc_id + chunk_id
  → DocumentService 校验当前 tenant 与成员权限
  → 校验 chunk_id 属于 doc_id
  → 从当前 Document 索引读取 Chunk
  → 返回正文与定位元数据
```

## 5. 异常

| 情况 | 行为 |
|---|---|
| Document、Chunk 不存在或不属于当前 tenant | `not_found` |
| `chunk_id` 不属于 `doc_id` | `not_found` |
| Document 尚未 ready | `document_not_ready` |

## 6. 验收

```text
□ 输入仅为 doc_id + chunk_id
□ 输出包含 doc_id + chunk_id + content_hash
□ 同一 chunk_id 不返回不同内容
□ 权限与 search_documents、search_chunks 一致
```
