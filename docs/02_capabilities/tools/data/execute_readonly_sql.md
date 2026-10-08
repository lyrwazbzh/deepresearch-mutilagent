# execute_readonly_sql

> 公共调用链见 [tool.md](../../tool.md)。数据库写入的独立闭环见 [database_write.md](../../database_write.md)。

## 1. Tool 定位

- 在**业务库**执行只读分析 SQL。
- 三分立：

```text
inspect_schema        → 有什么表、什么字段？
sample_table          → 表里真实数据长什么样？
execute_readonly_sql  → 真正执行分析查询
```

```text
✓ 写库 → WritePlan → HITL → WriteExecutor（非本 Tool）
```

## 2. 使用场景

- 调用：聚合、趋势、只读 join/分析。

## 3. 输入

```text
sql: string                # 必填；整条由模型生成的 SQL（非整句 parameter binding）
datasource_id?: string
  # 当前用户只有一个 enabled 且获授权的 datasource 时可省略；
  # 多数据源时必须显式指定
row_limit?: int = 200      # 服务端 hard limit = 1000
timeout_ms?: int = 10000   # 服务端 hard limit = 30000
```

`row_limit <= 0`、`row_limit > 1000`、`timeout_ms <= 0` 或 `timeout_ms > 30000` → `invalid_argument`。DB 连接同步设置 `statement_timeout = timeout_ms`。`row_limit` 决定本次调用最多可取得和返回的数据量；inline budget 只决定这部分允许结果是直接返回还是写入 Workspace。适合当前输出预算且不需要文件消费的完整结果直接返回；长表格或明确作为后续文件输入的结果写入 Workspace，ToolResult 返回预览和 `workspace_path`。


```text
✓ 安全靠：AST / lineage 校验 + DataSource Policy + DB 只读账号 + timeout + row limit
```

## 4. 输出

```text
columns: string[]
rows: any[][] | preview    # 过小直接 rows；过大可仅 preview + workspace_path
truncated?: bool            # 因 hard row limit 截断
workspace_path?: string    # 大结果或明确作为文件输入时写入 Workspace
content_hash: string       # inline 时为规范化 columns/rows；Workspace 时为文件字节
```

语义总结由 DataAnalyst 完成；Tool 负责返回结果结构或 Workspace 引用。

## 5. 正常主流程

```text
公共链
  → 解析 datasource_id（显式 or 当前用户唯一获授权项；否则 invalid_argument）
  → datasource scope / ownership 校验
  → SQL Parser → AST
  → 校验为单 Statement
  → 校验顶层为 SELECT / 只读 WITH
  → 解析 JOIN / CTE / subquery / alias / SELECT *
  → 还原所有真实 schema.table.column lineage
  → DataSource Policy 校验 table policy + readable_columns
  → 只读 DB 连接 / read-only transaction 执行
  → timeout + hard row limit；只取得 row_limit 范围内的允许结果
  → 小结果直接 ToolResult
  → row_limit 范围内的允许结果超过 inline budget 或明确需要文件消费
       → Workspace 表格文件
       → workspace_path
  → ToolResult
```

SQL/Data 模块负责 AST 与 lineage 解析；DataSource Policy 只消费解析后的真实表列集合做授权判定。部分列授权时，`SELECT *` 只有在展开后的全部列均属于 `readable_columns` 才能执行。

双保险：

```text
应用层 AST 校验（防误调用）
+
数据库只读账号 / read-only transaction（最终兜底）
```

## 6. 底层依赖

- 只读 DB 凭证（L_resource）
- SQL Parser / AST 校验器
- Workspace（大结果快照）

## 7. 权限

- DataAnalyst
- datasource grant + schema/table/column policy

## 8. 异常与降级

| 情况 | 行为 |
|------|------|
| DataSource/grant/table/column 无权限 | `policy_denied` |
| 多 datasource 缺 id | `invalid_argument` |
| SQL 无法解析、不是受支持的只读查询或执行语义非法 | `invalid_argument` |
| timeout | `ToolError(type=timeout)`；是否 retry 由 ToolRuntime 按只读调用策略决定 |
| 连接、DNS、TLS 或数据库服务不可用 | `provider_unavailable`；具体 `cause` 仅进入失败事件或日志 |
| 结果超过 inline budget | 有效 row limit 范围内的完整允许结果落 Workspace + `workspace_path` |
| 结果超过有效 row limit | `truncated=true`；该标记随 Evidence 保留 |

## 9. 状态与副作用

```text
DB：只读
小结果：直接返回
大结果 / 后续文件输入：写 Research Workspace 工作记忆
State：经 ToolResult → research_rounds[depth].sql → MergeEvidence
```

## 10. 可观测

```text
datasource_id, row_count, truncated,
validation_failed?
```

## 11. 测试

```text
□ 合法 SELECT / WITH…SELECT
□ policy 外表 → policy_denied
□ 非 readable column、部分列授权下 SELECT * → policy_denied
□ timeout；row_count = 实际返回行数
□ JOIN / CTE / subquery / alias 均还原为真实 schema.table.column 后执行 policy 校验
□ 实际结果超过有效 row limit 时仅保留允许范围并置 truncated=true；Workspace 不绕过 row_limit
```

## 12. 验收标准

```text
□ AST 单语句只读 + schema/table/column policy + DB 只读账号双保险
□ datasource_id 省略条件与 inspect_schema 一致
□ row_limit 限制取得的数据量；inline budget 只决定内联或 Workspace
□ 大结果 → 有效 row limit 范围内的 Workspace snapshot + workspace_path
□ Tool 返回结构化 rows/preview，语义总结由 DataAnalyst 完成
□ inline 结果保留 columns/rows/truncated，Workspace 结果保留允许范围内的完整文件与预览
□ 与 inspect_schema / sample_table / query_dataset 职责范围清晰
□ row_limit 默认 200/hard 1000；timeout 默认 10,000ms/hard 30,000ms；DB statement_timeout 同步
```
