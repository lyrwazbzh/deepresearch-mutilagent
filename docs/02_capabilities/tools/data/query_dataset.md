# query_dataset

> 公共调用链与 Dataset 绑定见 [tool.md](../../tool.md) 和 [inspect_dataset.md](inspect_dataset.md)。

## 1. 定位

在 SQL/Data 分支中，对已绑定 Dataset 的一个逻辑表执行受限 DuckDB SELECT。外部业务数据库查询由 `execute_readonly_sql` 处理；两者进入 ResearchState 后统一作为 SQL 结果。

DataAnalyst 根据 hypothesis、Dataset schema 和已有结果生成 `query`；`query_dataset` 不调用 LLM，也不负责 Text2SQL，只负责 SQL AST 校验、资源约束、DuckDB 执行和 ToolResult 返回。

## 2. 输入

```text
dataset_ref: string
table?: string
query: string
row_limit?: int = 100
```

`table` 在单表 Dataset 中可省略；XLSX 包含多个 Sheet 时指定对应逻辑表。选中的逻辑表在 DuckDB 会话中映射为固定只读 View `dataset`：

```sql
SELECT region, SUM(sales) AS total
FROM dataset
GROUP BY region
```

`row_limit` 最大为 1000。

## 3. 输出

```text
columns[]
rows[][]
row_count
truncated?
workspace_path?
content_hash
```

```text
结果适合 ToolResult 预算
  → 完整 columns / rows / row_count / truncated 写入 ToolResult.data

结果超过预算或明确作为文件输入
  → 写入 Workspace
  → 返回 summary + workspace_path + content_hash
```

## 4. 主流程

```text
dataset_ref + table
  → Ownership / scope
  → Dataset Catalog 解析 Parquet 逻辑表
  → DuckDB 创建临时只读 View dataset
  → SQL AST 与资源来源校验
  → 执行 SELECT
  → 完整短结果直接返回 / 长结果或后续文件输入写 Workspace
  → ToolResult
  → DataAnalyst 采纳与整理
  → research_rounds[depth].sql
  → MergeEvidence
  → evidence_pool
```

SQL/Data 模块使用统一 AST 解析能力校验单条 `SELECT` 或 `WITH … SELECT`，解析 JOIN、CTE、子查询及所有底层 relation / table source。查询只能读取绑定后的固定只读 `dataset` View；其他表、文件读取函数、外部资源和可产生副作用的语句均不通过校验。

`query_dataset` 不直接写入 `evidence_pool`；Dataset 查询结果作为 SQL 分支结果，由 `MergeEvidence` 生成正式 Evidence。

## 5. 异常

| 情况 | 行为 |
|---|---|
| Dataset 或逻辑表不存在 | `not_found` |
| Dataset 尚未完成解析 | `ToolError(type=conflict, code=dataset_not_ready)` |
| SQL 非法 | `invalid_argument` |
| SQL 访问未绑定资源 | `policy_denied` |
| 超时 | `timeout` |
| 结果超过 row limit | 截断并返回 `truncated=true` |

`dataset_not_ready` 是统一 `ToolError` 下的 SQL/Data 业务错误码，不扩展为新的顶层错误类型。

## 6. 验收

```text
□ Dataset 属于 SQL/DataAnalyst 分支
□ Dataset 查询结果先进入 research_rounds[depth].sql，再由 MergeEvidence 生成 SQL Evidence，不形成独立 Dataset 来源类型
□ Dataset 原文件与 Parquet 解析由 SQL/Data 模块管理
□ DuckDB 只创建查询期临时只读 View
□ 业务数据库保持原有表结构，Dataset 查询使用 DuckDB 临时 View
□ 完整短结果进入 data，长结果或后续文件输入写 Workspace
□ DataAnalyst 生成 query；query_dataset 不调用 LLM 或执行 Text2SQL
□ AST 校验解析全部底层资源来源，查询只能读取绑定的 dataset View
□ 超时使用统一 timeout；dataset_not_ready 使用统一 ToolError 的 type + code
```
