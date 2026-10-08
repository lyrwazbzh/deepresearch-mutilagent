# DeepResearch-mutilagent：DataSource

## 0. 定位

DataSource 表示外部 PostgreSQL 业务数据库，负责连接元数据、凭据引用、用户授权以及表列策略。

```text
DataAnalyst 读链
  → inspect_schema / sample_table / execute_readonly_sql
  → DataSource scope + grant + policy
  → CredentialResolver(mode=read)

数据库写链
  → WritePlan / HITL / WriteExecutor
  → DataSource scope + grant + policy
  → CredentialResolver(mode=write)
```

数据库写合同见 [`database_write.md`](database_write.md)，只读 Tool 见 [`tools/data/`](tools/data/)，Secret 合同见 [`auth_security.md`](../04_platform/auth_security.md)。

---

## 1. P0 支持范围

```text
PostgreSQL
服务器 TLS 校验
tenant-owned DataSource
显式 user grant
read/write 独立数据库 Principal
schema/table/column policy
固定外部 Ledger 幂等
```

---

## 2. 数据合同

### 2.1 datasources

```text
datasources
  datasource_id
  tenant_id
  name
  host
  port
  database_name
  ssl_mode
  credential_ref
  enabled
  created_at
  updated_at
```

字段职责：

- `datasource_id` 是 Tool、Grant、Policy、PendingWrite 和 Audit 的稳定引用。
- `name` 在 tenant 内唯一，用于 UI 展示和 DataAnalyst 识别。
- `host/port/database_name/ssl_mode` 是连接元数据。
- `credential_ref` 指向 SecretsService 中的数据库凭据包。

### 2.2 Credential Bundle

```text
credential_ref
  → DatabaseCredentialBundle {
      read: {
        username
        password
      }

      write?: {
        username
        password
      }
    }
```

`read` Principal 用于 schema、sample 和只读 SQL；`write` Principal 仅由 WriteExecutor 解析。凭据包中存在 `write` Principal 时，DataSource 具备数据库层写入条件。

SecretsService 使用 `MASTER_KEY` 加密凭据包，DataSource 只保存 `credential_ref`。凭据解析接口为：

```text
CredentialResolver.resolve(credential_ref, mode=read|write)
```

### 2.3 datasource_user_grants

```text
datasource_user_grants
  tenant_id
  datasource_id
  user_id
  access_level: read | write
  created_at

unique(tenant_id, datasource_id, user_id)
```

```text
read
  → inspect_schema / sample_table / execute_readonly_sql

write
  → 包含 read 能力
  → 可生成 WritePlan
  → 经 HITL 与 WriteExecutor 执行
```

### 2.4 datasource_table_policies

```text
datasource_table_policies
  tenant_id
  datasource_id
  schema_name
  table_name
  readable_columns[]
  writable_columns[]
  allow_insert
  allow_update
  allow_delete
  max_write_rows

unique(tenant_id, datasource_id, schema_name, table_name)
```

`max_write_rows` 是实际数据库写入影响行数的唯一策略上限。WriteExecutor 在事务内取得 `affected_rows` 后应用该上限。

---

## 3. Scope 与授权

DataSource 访问条件：

```text
datasource.tenant_id == AuthContext.tenant_id
AND datasource.enabled == true
AND 当前 user 具有 datasource_user_grants
AND 目标 schema/table/column/operation 符合 datasource_table_policies
```

Tool 调用权限由 Agent Tool Policy 处理；DataSource 层只处理用户、资源和数据库策略。

DataSource 选择：

```text
当前用户只有一个获授权 DataSource
  → 服务端可解析该 DataSource

当前用户有多个获授权 DataSource
  → 请求提供 datasource_id
```

Task 创建请求可以把本次研究限定在用户已获授权的 DataSource 子集：

```text
POST /tasks options.datasource_ids
  → ResearchTask capability_snapshot.datasource_ids
```

该快照只表达“本次 Task 最多可以使用哪些 DataSource”，不是授权凭据，也不形成 `TaskDataSourceGrant` 或第二套授权表。每次 SQL 执行都取 Task scope 与当前有效授权的交集，并从权威 DataSource 记录重新校验 `tenant + enabled + user grant + table policy`；Task 快照不能扩大或替代当前授权。

---

## 4. 连接池

```text
pool key = (datasource_id, access_mode, datasource.updated_at)
access_mode = read | write
```

```text
datasource_id
  → tenant / enabled / grant / policy
  → credential_ref
  → Adapter 在执行时 CredentialResolver.resolve(mode)
  → read pool 或 write pool
  → 执行
```

read/write 使用不同数据库 Principal 和连接池。凭据不在登录或 Task 启动时解析；只在实际 SQL 执行已通过授权与策略校验后，由 DataSource Adapter 按需解密。DataSource 或 credential 更新会更新 `datasource.updated_at`，后续调用建立新池。连接池配置 connect、pool、statement timeout 和健康检查。

---

## 5. Read Policy

```text
SQL/Data 模块解析 SQL AST
  → resolve JOIN / CTE / alias / subquery
  → 得到真实 schema.table.column 与 operation
  → DataSource Policy 消费解析结果
  → 解析 datasource_id
  → tenant scope
  → enabled
  → user grant(read|write)
  → datasource_table_policies
  → CredentialResolver(mode=read)
  → database-side read-only transaction
  → timeout / row limit
```

真实表使用 `schema.table` 标识。SQL/Data 能力统一负责 SQL AST 与 lineage 解析，Text2SQL、只读执行和写入检查复用同一解析结果；DataSource Policy 只根据解析后的真实表、列与 operation 做 grant/policy 判定，不实现第二套 SQL parser。

具体输入输出、row limit 与 timeout 由各 Tool 合同定义。

---

## 6. Write Policy 与固定 Ledger

数据库写入条件：

```text
user grant = write
AND CredentialResolver(mode=write) 成功
AND table policy 允许 operation
AND values / filters 使用 writable columns
AND HITL 已批准
```

P0 对 INSERT、UPDATE、DELETE 统一使用外部 Ledger，并与业务 DML 位于同一目标数据库事务：

```text
WRITE_LEDGER_TABLE=public.agent_write_idempotency
```

```text
agent_write_idempotency
  operation_key      PRIMARY KEY
  plan_hash
  affected_rows
  result_json
  committed_at
```

Ledger 表名由应用配置统一管理。DataSource 启用写能力时，管理流程验证表结构以及 write Principal 的访问权限。

WriteExecutor 根据 [`database_write.md`](database_write.md) 完成参数化 SQL、precondition、`Policy.max_write_rows`、Ledger、事务和 Audit。

---

## 7. 管理生命周期

```text
Tenant Admin 提交连接元数据与 DatabaseCredentialBundle
  → 网络出口策略检查
  → SecretsService 加密凭据包
  → 创建 datasource(enabled=false)
  → 使用 read Principal 执行 SELECT 1
  → 存在 write Principal 时验证固定 Ledger 表结构与权限
  → 配置 table policies 与 user grants
  → enable
```

配置、凭据、Grant、Policy 和启停变化进入 Audit。`enabled=false` 后，新的读写解析返回 `policy_denied`，连接池按更新后的 DataSource revision 重建。

`write_ready` 是服务端派生值：

```text
enabled
+ credential bundle 包含 write Principal
+ Ledger 校验成功
+ 当前用户具有 write grant
+ 至少一项 table policy 允许写 operation
```

`write_ready` 按当前 DataSource、Credential、Ledger、用户 Grant 与 Policy 实时生成，只出现在 API projection，不持久化为 `datasources.write_ready` 字段。

---

## 8. API Contract

Tenant Admin：

```text
POST   /api/v1/datasources
GET    /api/v1/datasources
GET    /api/v1/datasources/{datasource_id}
PATCH  /api/v1/datasources/{datasource_id}
POST   /api/v1/datasources/{datasource_id}/test
POST   /api/v1/datasources/{datasource_id}/enable
POST   /api/v1/datasources/{datasource_id}/disable
PUT    /api/v1/datasources/{datasource_id}/policies
PUT    /api/v1/datasources/{datasource_id}/grants/{user_id}
DELETE /api/v1/datasources/{datasource_id}/grants/{user_id}
```

普通用户：

```text
GET /api/v1/datasources
  → datasource_id / name / access_level / enabled / write_ready
```

---

## 9. 可观测与错误

```text
datasource_id
tenant_id
user_id
task_id?
access_mode
schema.table
operation
policy_result
row_count / affected_rows
error_class
```

错误分类：

```text
disabled
policy_denied
invalid_argument
connection_failed
timeout
ledger_unavailable
```

---

## 10. 测试

```text
□ credential_ref 可分别解析 read 与 write Principal
□ read/write 使用不同连接池
□ 多 DataSource 场景按 datasource_id 解析授权资源
□ Task capability_snapshot.datasource_ids 只缩小范围；每次执行仍校验当前 enabled、Grant 与 Policy，不存在第二套 Task 授权
□ JOIN / CTE / subquery 的真实表列均经过 read policy
□ SQL/Data 模块统一解析 AST/lineage，DataSource Policy 只消费真实资源集合做授权判定
□ write grant、table policy 与 write Principal 共同派生 write_ready
□ write_ready 仅为按用户实时生成的 API projection，不持久化到 datasources
□ DataSource 启用写能力时验证固定 Ledger 表与权限
□ WriteExecutor 使用 Policy.max_write_rows 控制实际 affected_rows
□ DataSource、credential、Grant 与 Policy 更新后连接池和授权即时生效
```

---

## 11. 验收标准

```text
□ DataSource 表达外部 PostgreSQL 连接、用户授权与表列策略
□ 一个 credential_ref 管理 read/write 独立 Principal
□ Agent Tool Policy 与 DataSource 资源授权职责分离
□ Policy.max_write_rows 是写入影响行数的唯一策略上限
□ P0 数据库写入统一使用固定外部 Ledger
□ Read Policy、Write Policy、连接池、管理生命周期和 Audit 可直接实现
```
