# inspect_schema

> 公共调用链见 [tool.md](../../tool.md)。

## 1. Tool 定位

- 探查业务库表/字段/类型（schema discovery），为数据查询做准备。
- 三分立：

```text
inspect_schema        → 有什么表、什么字段？
sample_table          → 表里真实数据长什么样？
execute_readonly_sql  → 真正执行分析查询
```

## 2. 使用场景

DataAnalyst 在生成 SQL 前使用该 Tool 获取可访问表、字段、类型、主键与关系。

## 3. 输入

```text
datasource_id?: string
  # 当前用户只有一个 enabled 且获授权的 datasource 时可省略；

table?: string
  # 逻辑标识 = schema.table（例 sales.orders）
  # 省略 → 列出当前允许访问的表
  # 有值 → 返回该表列元数据

limit?: int = 100
  # 仅 table 省略时生效；服务端 hard limit = 500
cursor?: string
  # 仅 table 省略时的分页游标
```


## 4. 输出

`table` 省略：

```text
datasource_id
tables: [{
  schema: string
  name: string
}]
next_cursor?: string
truncated: bool
```

`table` 有值（`schema.table`）：

```text
datasource_id
columns: [{
  schema: string
  table: string
  name: string
  type: string
  nullable?: bool
}]
primary_key?: string[]
foreign_keys?: [{
  columns: string[]
  referenced_schema: string
  referenced_table: string
  referenced_columns: string[]
}]
```

`primary_key` 与 `foreign_keys` 只返回当前 DataSource Policy 允许暴露的表和列关系；不满足访问范围的关系不进入 ToolResult。

```text
✓ 二选一：列表表 vs 单表列
```

## 5. 正常主流程

```text
公共链
  → 解析 datasource_id（显式 or 当前用户唯一获授权项；否则 invalid_argument）
  → datasource scope / ownership 校验
  → schema/table/column policy
  → PostgreSQL information_schema / pg_catalog
  → 按 table policy 过滤为 table + readable_columns metadata
  → table 省略时按 schema/name 稳定排序并应用 limit/cursor
  → ToolResult
```

```text
✓ 最终结果来自 allowlist 内 schema/table（尤其 table 为空的列表场景）
```

## 6. 底层依赖

- 只读 DB 凭证（L_resource，见 [tool.md](../../tool.md) / [auth_security.md](../../../04_platform/auth_security.md)）
- schema/table/column policy；PostgreSQL metadata 目录

## 7. 权限

- DataAnalyst
- 仅当前 datasource 下 policy 允许的 schema/table/readable_columns
- 表名须用 `schema.table` 消歧（多 schema 同名表）

## 8. 异常与降级

| 情况 | 行为 |
|------|------|
| 多 datasource 且未传 datasource_id | `invalid_argument` |
| DataSource/grant/table/column 无权限 | `policy_denied` |
| 连接失败 | `provider_unavailable` |

连接、DNS、TLS 等底层原因遵循统一 `ToolError` 合同，仅作为失败事件或日志中的 `cause`，不扩展 `inspect_schema` 专用错误类型。

## 9. 状态与副作用

该 Tool 读取 DataSource 元数据并返回结构化 schema。

## 10. 可观测

```text
datasource_id
result_count          # tables.length 或 columns.length
```

## 11. 测试

```text
□ table 省略 → 仅 allowlist 内 tables[{schema,name}]
□ table=sales.orders → columns[{schema,table,…}] + primary_key? + foreign_keys?
□ archive.orders vs sales.orders 可区分
□ 当前用户有多个获授权 datasource 且缺 datasource_id → invalid_argument
□ 主键与外键关系经过 table/column policy 过滤
□ 连接失败统一返回 provider_unavailable，具体 cause 仅进入失败事件或日志
```

## 12. 验收标准

```text
□ table 逻辑标识 = schema.table；列表/列模式互斥清晰
□ datasource_id 省略条件钉死；否则必填
□ 表列表分页有界且排序稳定
□ 单表模式的主键与外键输出合同可供 DataAnalyst 生成 JOIN
```
