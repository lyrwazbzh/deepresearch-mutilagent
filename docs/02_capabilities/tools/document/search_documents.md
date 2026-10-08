# search_documents

> 公共调用链见 [tool.md](../../tool.md)，Document 数据合同见 [document_system.md](../../document_system.md)。

## 1. 定位

在当前 tenant 知识库内发现相关文件：

```text
search_documents → 找相关文件
search_chunks    → 找相关切片
read_document    → 按 doc_id + chunk_id 读取切片
```

## 2. 输入

```text
query?: string
filters?:
  tags?: string[]
limit?: int = 20
cursor?: string
```

`query` 对 `name`、`tags` 和可选文档摘要做轻量检索。空查询返回当前 tenant 知识库中最近更新的 Document。

## 3. 输出

```text
documents[]:
  doc_id
  name
  media_type
  tags[]?
  updated_at
  relevance_score?
next_cursor?
truncated
```

只返回 `status=ready` 且属于当前 tenant 的 Document。

## 4. 主流程

```text
AuthContext
  → DocumentService 解析可读取范围
  → tags 过滤
  → 可选 name / metadata 轻量检索
  → 排序与分页
  → ToolResult
```

## 5. 验收

```text
□ 字段与 documents 合同一致
□ 查询范围由 DocumentService 按当前身份约束
□ 与 search_chunks、read_document 职责分离
□ limit、cursor、next_cursor 与 truncated 可验证
```
