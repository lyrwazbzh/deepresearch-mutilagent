# sandbox_execute

> 公共调用链见 [tool.md](../../tool.md)，Workspace 合同见 [research_workspace.md](../../research_workspace.md)，状态字段见 [state.md](../../../01_architecture/state.md)。

## 1. Tool 定位

- 由 **CodeWizard** 在 Post-Merge Analysis Decision 产生 `AnalysisRequest` 且输入 Evidence 已就绪时调用。


```text
调用：统计计算、CSV/JSON 转换、复杂数据加工、图表数据准备
        标准图/表渲染 → generate_chart / render_table
```

## 3. 输入

Agent 可见 schema：

```text
analysis_id: string               # 当前 AnalysisRequest
code: string
inputs?: [{
  name: string                    # 本次调用内唯一逻辑名
  evidence_id?: string            # 与 dataset_ref 二选一
  dataset_ref?: string            # SQL/Data 模块签发的受控 Dataset 逻辑引用
}]
timeout_ms?: int = 120000         # 可降低；服务端 hard limit = 300000
```

```text
evidence_id
  → Evidence
  → data、doc_id+chunk_id 或 workspace_path
  → ResourceResolver

dataset_ref
  → Dataset Catalog
  → 对应的 Workspace-backed Dataset 文件
  → ResourceResolver

Runtime 将解析后的输入映射为只读 /inputs/<name>
Runtime 提供空的可写 /outputs
代码通过环境变量 DEEPRESEARCH_INPUT_MANIFEST / DEEPRESEARCH_OUTPUT_DIR 定位
```

`dataset_ref` 不是 `workspace_path`，Sandbox 不直接解析其物理文件位置。Dataset 可以作为补充执行输入；要进入 Compute branch 的输出仍须由当前 AnalysisRequest 的至少一个 Evidence 支撑，后续 `derived_from` 只保存这些 `evidence_id`。

固定资源范围：

```text
CPU                    2 vCPU
Memory                 2 GiB
Writable disk          2 GiB
PIDs                    64
Wall time default      120s
Wall time hard limit   300s
stdout + stderr        4 MiB
单个输出文件           256 MiB
单次输出文件总量       512 MiB
```

`timeout_ms <= 0` 或 `timeout_ms > 300000` → `invalid_argument`。

## 4. 输出

```text
exit_code: int
stdout: string                      # 受限运行诊断，不进入 Evidence
stderr: string                      # 受限运行诊断，不进入 Evidence
truncated_output: bool
lineage_manifest_path: string       # compute/{tool_call_id}/manifest.json
files: [{
  workspace_path: string            # compute/{tool_call_id}/outputs/**
  content_hash: string
  media_type?: string
  bytes: int
}]
resource_usage?: { runtime_ms, peak_memory_bytes? }
```


## 5. 正常主流程

```text
公共 Tool 链
  → 校验 caller=CodeWizard、analysis_id 对应 pending AnalysisRequest
  → 校验输入属于该 AnalysisRequest；解析 Evidence 或 DatasetRef
  → 建立隔离执行环境：只读 /inputs、可写 /outputs、network=false
  → 执行 Python，并施加 CPU/memory/disk/process/time/output 限额
  → 收集并截断 stdout/stderr
  → 校验 /outputs；发布 code.py、manifest.json 与 outputs/**
  → ToolResult
  → CodeWizard 检查、解释并采纳已发布输出
  → 每个 adopted output 形成包含 Hypothesis 判断与 result_key 的 Compute branch result
  → Graph Node 提交 research_rounds[depth].compute
```

Sandbox 不返回内联研究结果。代码需要把计算结果写入 `/outputs`；未产生输出文件时，本次调用只提供执行状态与诊断，不形成 Evidence。`manifest.json` 遵循 [`research_workspace.md`](../../research_workspace.md) 的唯一 Compute Manifest 合同，分别记录 `input_evidence[]` 与 `input_datasets[]`；Dataset 引用不写入 `derived_from`。MergeEvidence 再根据 adopted Compute branch result 生成带 `derived_from + execution_ref` 的 Compute Evidence。


## 6. 底层依赖

- DocumentService / WorkspaceService / Resource Resolver
- 公共 Policy、ToolResult 与 OTel 链

## 7. 权限与安全

```text
Allowed Agent：CodeWizard
Network：false（P0 固定）
Business DB：无凭证、无直连
```

## 8. 异常与降级

| 情况 | 行为 |
|---|---|
| timeout / OOM / 磁盘或进程超限 | 终止执行，返回对应 `tool_error` 分类 |

## 9. 状态与副作用

- 输出文件写入当前 Task Workspace；`sandbox_execute` 只返回 ToolResult，CodeWizard 负责采纳与解释输出，Graph Node 再将 adopted results 写入 `research_rounds[depth].compute`。同一逻辑调用重放复用已发布 bundle，MergeEvidence 按稳定 `result_key` 复用 Evidence。

## 10. 可观测

```text
tool_call_id, exit_code, runtime_ms, input_count, output_count,
stdout_bytes, stderr_bytes, truncated_output, limit_reason?, network_enabled=false
```


## 11. 测试

```text
□ 正常计算并将 /outputs 文件原子提交到 Workspace
□ manifest 能从每个输出分别回溯输入 Evidence、Dataset、代码和运行环境
□ stdout / stderr 仅供诊断；无输出文件时不形成 Evidence
□ timeout、OOM、磁盘/进程/输出超限均能终止且分类正确
□ ToolResult 先由 CodeWizard 采纳与解释，再由 Graph Node 写入 research_rounds[depth].compute
□ dataset_ref 经 Dataset Catalog 解析，不作为 workspace_path 或 derived_from 使用
```

## 12. 验收标准

```text
□ CodeWizard 只执行 Post-Merge Analysis Decision 产生的 AnalysisRequest
□ Sandbox 计算结果始终通过 Workspace 文件交付；只有 CodeWizard 采纳的输出形成 Compute branch result
□ Dataset 输入记录在 manifest.input_datasets；Compute Evidence 的 derived_from 仍只包含 Evidence ID
□ 2 vCPU/2 GiB RAM/2 GiB disk/64 PIDs；120s 默认/300s hard；日志4 MiB
□ 单文件≤256 MiB、单次输出总量≤512 MiB；网络固定关闭
□ 错误分类、幂等提交、Trace 与测试闭环完整
```
