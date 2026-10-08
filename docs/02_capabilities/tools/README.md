# Tools 实现文档

公共调用链、ToolResult、allowlist、幂等和长结果外置规则见 [`../tool.md`](../tool.md) 与 [`../../03_runtime/runtime.md`](../../03_runtime/runtime.md)。

```text
tools/
├─ web/              # search_web / read_webpage / render_page
├─ document/         # search_documents / search_chunks / read_document
├─ data/             # SQL 与 Dataset
├─ compute/          # Sandbox / Chart / Table
└─ knowledge_graph/  # Task KG
```

各 Tool 文档描述：

```text
定位
输入
输出
主流程
依赖
资源范围
错误与恢复
状态与副作用
可观测
测试与验收
```

统一结果处理：

```text
Web / SQL 完整短结果 → ToolResult.data
Web / SQL 长结果 → WorkspaceService.finalize_file → workspace_path
Compute 结果与文件 → WorkspaceService.finalize_file → workspace_path
知识库结果 → doc_id / chunk_id
Graph Node → research_rounds
MergeEvidence → evidence_pool
```

Workspace 是当前 Task 的工作记忆文件系统，由各 producer 直接写入；Document 是可跨 Task 检索的知识库。
