# render_table

> 公共调用链见 [tool.md](../../tool.md)，Workspace 合同见 [research_workspace.md](../../research_workspace.md)。

## 1. Tool 定位

- 将结构化数据渲染为报告/UI 可引用的表格产物。


```text
调用：公司/政策对比、矩阵、结果汇总表需要持久化引用
```

## 3. 输入

```text
# inline 与 data_ref 二选一
inline?: { columns: string[], rows: any[][] }
data_ref?: {
  workspace_path?: string
  doc_id?: string
  chunk_id?: string
}
evidence_ids: string[]                # 非空；表格数据所依据的上游 Evidence

title?: string
format?: markdown | html | csv       # 默认 markdown
max_rows?: int = 100                 # 可提高到服务端 hard limit 500
```

Runtime 注入 `tenant_id / user_id / task_id / node_run_id / tool_call_id`；`tool_call_id` 由 `node_run_id` + 稳定 table slot 派生，retry 不变。展示范围：

```text
默认展示行数       100
行 hard limit      500
列 hard limit      30
单 cell 展示上限   4 KiB UTF-8
最终序列化表格上限 256 KiB
```

`max_rows <= 0` 或 `max_rows > 500` → `invalid_argument`。

## 4. 输出

```text
workspace_path: string               # 当前 Task 的 Workspace 表格文件
content_hash: string
media_type: string
source_row_count: int
rendered_row_count: int
truncated: bool
evidence_ids: string[]
lineage_manifest_path: string          # compute/{tool_call_id}/manifest.json
```

`data_ref` 复用 [`tool.md`](../../tool.md) 的 ResourceResolver 输入：`workspace_path` 与 `doc_id + chunk_id` 二选一，不定义 `render_table` 专用的 `{type, ref}` 寻址协议。

若行、列、cell 或序列化大小超过展示范围，按稳定顺序生成明确标注“已截断”的 Workspace 表格并置 `truncated=true`。完整输入继续由上游 Evidence 的 `data` 或 `workspace_path` 提供，渲染器不复制第二份源数据。

## 5. 正常主流程

```text
公共 Tool 链
  → Policy 校验 CodeWizard
  → 校验 inline xor data_ref；data_ref 使用 workspace_path 或 doc_id + chunk_id 并通过 ResourceResolver
  → 校验 evidence_ids 属于当前 Task
  → 规范化 columns/rows，校验行列一致
  → 按 100 default / 500×30 / cell 4KiB / 256KiB 规则生成预览
  → 转义 Markdown/HTML/CSV 特殊字符
  → 必要时生成带显式截断标记的预览
  → 写入 manifest 与表格，原子发布 Compute bundle
  → ToolResult
```

## 6. 底层依赖

- WorkspaceService；Document/Workspace Resource Resolver；成熟的序列化/HTML escape 库。

## 7. 权限

- CodeWizard；输入须来自已授权的当前 Task Workspace 或 Document。

## 8. 异常与降级

| 情况 | 行为 |
|---|---|
| inline/data_ref 都有或都无 | `invalid_argument` |
| 行列不一致、重复/空列名、未知格式 | `invalid_argument` |
| 行数超过预览上限 | 生成显式截断表，返回 `truncated=true` |

## 9. 状态与副作用

- 表格文件写入当前 Task Workspace；CodeWizard 将 ToolResult 采纳为 `research_rounds[depth].compute` 结果，再由 MergeEvidence 按 [`evidence_pipeline.md`](../../../01_architecture/evidence_pipeline.md) 创建正式 Derived Evidence。
- manifest 保存上游 `evidence_ids + content_hash`、行数与输出 hash；Writer 通过 Evidence 读取完整数据并引用事实来源。

## 10. 可观测

```text
format, source_row_count, rendered_row_count, n_cols,
truncated, output_bytes
```

## 11. 测试

```text
□ markdown/html/csv 输出可解析，特殊字符正确转义
□ 行列不一致与超限分类正确
□ 截断产物含显式标记，ToolResult 行数与 truncated 一致
□ 渲染结果返回稳定 workspace_path
□ DataAnalyst 不在 render_table Tool allowlist；CodeWizard 可以调用
□ data_ref 使用 workspace_path 或 doc_id + chunk_id，经 ResourceResolver 解析
□ 表格产物经 research_rounds.compute → MergeEvidence 进入 evidence_pool
```

## 12. 验收标准

```text
□ 只做表格渲染，与查询/图表职责分立
□ render_table 仅授权 CodeWizard，不扩展 DataAnalyst 的 SQL/Data Tool allowlist
□ data_ref 复用 Workspace / Document 正式引用，不维护 {type, ref} 协议
□ 产物可被 UI/Writer 引用并能回溯完整数据源
□ Workspace 表格产物通过 Compute branch 与 MergeEvidence 形成 Derived Evidence
□ 默认100行；hard 500行×30列、cell 4KiB、序列化256KiB
□ 截断时完整数据仍可通过上游 Evidence 的 data 或 workspace_path 读取
```
