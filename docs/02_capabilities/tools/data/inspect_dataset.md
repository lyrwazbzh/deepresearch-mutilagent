# inspect_dataset

> 公共调用链见 [tool.md](../../tool.md)。

## 1. 定位

在 SQL/Data 分支中探查 XLSX、CSV 或 Parquet Dataset 的表、列、类型、行数和缺失概览。

```text
inspect_dataset → 结构
profile_dataset → 分布与质量
query_dataset   → DuckDB SQL 查询
```

## 2. Dataset 绑定

```text
POST /tasks/{task_id}/data-files
  → 原始 XLSX / CSV / Parquet 保存为 Task-scoped Workspace File
  → SQL/Data 模块绑定 workspace_path，不重复保存原文件
  → CSV：必要时规范化为一个 Parquet 逻辑表
  → XLSX：每个 Sheet 必要时规范化为一个 Parquet 逻辑表
  → Parquet：校验 schema 后登记
  → 派生 Parquet 仍作为当前 Task 的 Workspace File 保存
  → 内部 Dataset Catalog 只登记 manifest、schema 与 Workspace 文件引用
  → 签发 dataset_ref
```

Workspace 是原始文件与派生 Parquet 的文件事实源；Dataset Catalog 只是 SQL/Data 对这些文件建立的可查询逻辑视图，`dataset_ref` 是该逻辑 Dataset 的受控引用。任务内已有的 Workspace 表格文件需要再次查询时，SQL/Data 模块直接绑定原 `workspace_path`，不复制文件。Dataset 解析与 DuckDB 查询均不在业务数据库创建表。

## 3. 输入

```text
dataset_ref: string
```

`dataset_ref` 是 SQL/Data 模块签发的受控逻辑引用。Runtime 注入 `tenant_id`、`user_id` 和 `task_id`。

## 4. 输出

```text
tables[]:
  name
  columns[]:
    name
    dtype
    null_count?
    null_ratio?
  row_count?
  sample_preview?
truncated?
```

CSV 与 Parquet 默认返回一个逻辑表；XLSX 每个 Sheet 返回一个逻辑表。Sample Preview 每个表最多 5 行。

## 5. 主流程

```text
dataset_ref
  → Ownership / scope
  → Dataset Catalog
  → DuckDB 读取 Parquet schema
  → 有界统计与 Sample Preview
  → ToolResult
```

## 6. 异常

| 情况 | 行为 |
|---|---|
| Dataset 不存在或不可访问 | `not_found` |
| Dataset 尚未完成解析 | `ToolError(type=conflict, code=dataset_not_ready)` |
| 文件解析失败 | `ToolError(type=tool_error, code=dataset_parse_failed)` |
| 超时或超预算 | 已取得完整 schema 时返回 `truncated=true`，否则 `tool_error` |

`dataset_not_ready` 与 `dataset_parse_failed` 只作为 SQL/Data 模块的具体错误码，不扩展为新的顶层 ToolError 类型。

## 7. 验收

```text
□ CSV、XLSX、Parquet 均由 SQL/Data 分支处理
□ XLSX 每个 Sheet 对应一个逻辑表
□ dataset_ref 由 SQL/Data 模块签发并解析
□ 原始文件只保存在 Task Workspace；Dataset Catalog 只登记逻辑视图与 Workspace 引用
□ dataset_not_ready / dataset_parse_failed 通过统一 ToolError 的 type + code 表达
□ Dataset 查询不在业务数据库创建表
```
