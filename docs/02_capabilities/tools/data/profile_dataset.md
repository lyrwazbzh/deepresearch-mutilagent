# profile_dataset

> 公共调用链与 Dataset 绑定见 [tool.md](../../tool.md) 和 [inspect_dataset.md](inspect_dataset.md)。

## 1. 定位

对 Dataset 的一个逻辑表执行确定性的描述统计、缺失率、分布和异常值画像。

DataAnalyst 决定要画像的逻辑表和列；`profile_dataset` 不调用 LLM，只执行确定性的 SQL / DuckDB 统计。`mean`、`std`、`quantiles`、`null_count`、`distinct_count`、`top_values` 和 IQR `outlier_count` 均由 DuckDB 计算，LLM 只可在 ToolResult 返回后解释结果。

## 2. 输入

```text
dataset_ref: string
table?: string
columns?: string[]
```

`table` 在单表 Dataset 中可省略；XLSX 包含多个 Sheet 时指定对应逻辑表。

## 3. 输出

```text
profiles[]:
  column
  dtype
  count
  null_count
  distinct_count?
  mean?
  std?
  min?
  max?
  quantiles?: {q25?, q50?, q75?}
  top_values?: [{value, count}]
  outlier_count?
columns_profiled[]
columns_skipped[]?
truncated?
```

数值列使用 IQR 1.5 倍规则统计异常值。未指定 `columns` 时按原始顺序画像最多 50 列；显式指定时最多 100 列。

## 4. 主流程

```text
dataset_ref + table
  → Ownership / scope
  → Dataset Catalog 解析逻辑表
  → DuckDB 临时只读 View
  → 标准化统计
  → 完整短结果写入 ToolResult.data
  → 长结果或明确作为文件输入时写 Workspace 并返回 workspace_path
  → DataAnalyst 采纳与整理
  → research_rounds[depth].sql
  → MergeEvidence
  → evidence_pool
```

`profile_dataset` 不直接写入 `evidence_pool`；正式 Evidence 只由 `MergeEvidence` 生成。

## 5. 异常

| 情况 | 行为 |
|---|---|
| Dataset 或逻辑表不存在 | `not_found` |
| Dataset 尚未完成解析 | `ToolError(type=conflict, code=dataset_not_ready)` |
| 超时或资源超限 | 返回已完成列并置 `truncated=true`；无完整列时返回 `tool_error` |

`dataset_not_ready` 是统一 `ToolError` 下的 SQL/Data 业务错误码，不扩展为新的顶层错误类型。

## 6. 验收

```text
□ Dataset 通过 DuckDB 临时只读 View 画像
□ 结果进入 SQL 分支；完整短结果进入 data，长结果进入 Workspace
□ 多 Sheet XLSX 可选择具体逻辑表
□ 输出与列数上限可验证
□ DataAnalyst 决定表与列；profile_dataset 只执行确定性 DuckDB / SQL 统计，不调用 LLM
□ profile_dataset 不直接写 Evidence；采纳结果先进入 research_rounds.sql，再由 MergeEvidence 生成 Evidence
□ dataset_not_ready 通过统一 ToolError 的 type + code 表达
```
