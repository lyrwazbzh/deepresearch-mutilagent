# generate_chart

> 公共调用链见 [tool.md](../../tool.md)，Workspace 合同见 [research_workspace.md](../../research_workspace.md)。

## 1. Tool 定位

- 将受控结构化数据确定性渲染为图，并写入当前 Task Workspace。


```text
调用：趋势、对比、构成、散点关系可视化
        复杂数据计算 → sandbox_execute 或 Data tools 先产出数据
        Task KG 网络图 → 后续可选扩展 network 类型
```

## 3. 输入

```text
chart_type: line | bar | pie | scatter

# data 与 data_ref 二选一
data?: { columns: string[], rows: any[][] }
data_ref?: {
  workspace_path?: string
  doc_id?: string
  chunk_id?: string
}
evidence_ids: string[]               # 非空；图中数据所依据的上游 Evidence

x: string
y: string | string[]
series?: string
title?: string
```

`data_ref` 复用 [`tool.md`](../../tool.md) 的 ResourceResolver 输入：`workspace_path` 与 `doc_id + chunk_id` 二选一，不定义 `generate_chart` 专用的 `{type, ref}` 寻址协议。

规则：

```text
line/bar：y 可为一个或多个数值列
scatter：y 必须为单个数值列
```

Runtime 注入 `tenant_id / user_id / task_id / node_run_id / tool_call_id`；`tool_call_id` 由 `node_run_id` + 稳定 chart slot 派生，retry 不变。规模约束：

```text
推荐 plotted points ≤ 5,000
hard limit          = 20,000 plotted points
series hard limit   = 20
类别轴 hard limit   = 200 categories
```


## 4. 输出

```text
workspace_path: string             # 当前 Task 的 Workspace 图表文件
content_hash: string
media_type: image/png
evidence_ids: string[]
lineage_manifest_path: string       # compute/{tool_call_id}/manifest.json
```

## 5. 正常主流程

```text
公共 Tool 链
  → Policy 校验 CodeWizard
  → 校验 data xor data_ref；data_ref 使用 workspace_path 或 doc_id + chunk_id 并通过 ResourceResolver
  → 校验 evidence_ids 属于当前 Task
  → 校验列存在、行列长度、图型字段规则和 hard limits
  → 写入 manifest 与图表，原子发布 Compute bundle
  → ToolResult
```

## 6. 底层依赖

- 成熟绘图库；WorkspaceService；Document/Workspace Resource Resolver

## 7. 权限

- CodeWizard；读取已授权的当前 Task Workspace 或 Document 数据。

## 8. 异常与降级

| 情况 | 行为 |
|---|---|
| data/data_ref 都有或都无 | `invalid_argument` |

## 9. 状态与副作用

- 图表文件写入当前 Task Workspace，Graph Node 保存引用。
- manifest 保存上游 `evidence_ids + content_hash` 和输出 hash；Report Image 通过这些 Evidence 引用事实来源。

## 10. 可观测

```text
chart_type, point_count, series_count, input_kind,
output_bytes
```

## 11. 测试

```text
□ line/bar/pie/scatter 正常渲染并返回稳定 workspace_path
□ 输出 hash 可复算，evidence_ids 可回到上游事实来源
□ DataAnalyst 不在 generate_chart Tool allowlist；CodeWizard 可以调用
□ data_ref 使用 workspace_path 或 doc_id + chunk_id，经 ResourceResolver 解析
```

## 12. 验收标准

```text
□ 单 Tool 覆盖 P0 四类图；没有额外 Chart Agent
□ 输入来源二选一、权限明确、正常链完整
□ generate_chart 仅授权 CodeWizard，不扩展 DataAnalyst 的 SQL/Data Tool allowlist
□ data_ref 复用 Workspace / Document 正式引用，不维护 {type, ref} 协议
□ 输出只进 Workspace，数据型图表通过 evidence_ids 回溯上游来源
□ 推荐≤5,000 points；hard≤20,000 points、20 series、200 categories
```
