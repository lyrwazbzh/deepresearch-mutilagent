# sample_table

> 公共调用链见 [tool.md](../../tool.md)。

## 1. Tool 定位

- 拉取某表少量真实样本行，辅助理解数据形态。

## 2. 使用场景

- 调用：写 SQL 前看样例。

## 3. 输入

```text
datasource_id?: string
  # 当前用户只有一个 enabled 且获授权的 datasource 时可省略；
  # 存在多个数据源时必须显式指定（与 inspect_schema / execute_readonly_sql 一致）
table: string              # 必填；逻辑标识必须为 schema.table
                           # 须在该 datasource allowlist 内
limit?: int                # 默认 5，最大 20
```

## 4. 输出

```text
datasource_id
table
columns: string[]          # 列名顺序
rows: any[][]              # 与 columns 对齐的二维数组
truncated?: bool            # 单元格过大被截断时
```

例：

```text
columns = ["id", "name", "price"]
rows = [
  [1, "A", 10],
  [2, "B", 20]
]
```

## 5. 正常主流程

```text
公共链
  → 解析 datasource_id（显式 or 当前用户唯一获授权项；否则 invalid_argument）
  → datasource scope / ownership 校验（Ownership / 只读连接）
  → 当前 DataSource policy：确认 table + readable_columns
  → limit = requested_limit ?? 5；≤0 或 >20 → invalid_argument
  → 仅投影显式 readable_columns：SELECT <quoted_columns> FROM <quoted_table> LIMIT n
  → 大单元格截断
  → ToolResult
```

```text
✓ policy 验证后显式列出并 quote schema/table/columns
```

## 6. 底层依赖

- 只读 DB 连接；schema/table/column policy；PostgreSQL quoting

## 7. 权限

- DataAnalyst；datasource grant + 表列 policy

## 8. 异常与降级

| 情况 | 行为 |
|------|------|
| 多 datasource 缺 id | `invalid_argument` |
| DataSource/grant/table 无权限 | `policy_denied` |
| timeout | `ToolError(type=timeout)`；是否 retry 由 ToolRuntime 按只读调用策略决定 |
| limit 非法（≤0） | `invalid_argument` |

## 9. 状态与副作用

该 Tool 以只读查询返回有界样本行。

## 10. 可观测

- `datasource_id`, `table`, `row_count`

## 11. 测试

```text
□ 正常样本（默认 limit=5）
□ limit>20 → invalid_argument；合法请求最多返回 20 行
□ 大单元格 truncated
```

## 12. 验收标准

```text
□ datasource_id 省略条件与 inspect_schema 一致；table 必须为 schema.table；limit 默认 5、最大 20
□ 先 datasource scope + policy 再 PostgreSQL-safe quote；只读显式列 SELECT … LIMIT
□ 与 inspect_schema / execute_readonly_sql 分离
```
