# DeepResearch-mutilagent：Share / Export

## 0. 定位

Share 与 Export 都以一个固定的 final report version 为输入：

```text
report.final
  ├─ Share  → 自包含的匿名只读 Snapshot
  └─ Export → DOCX / PDF 文件
```

```text
Share Snapshot / Asset / Export File → 当前 Task Workspace
shares / export_jobs                 → PostgreSQL 控制记录
文件 metadata                         → workspace_files
文件字节                              → MinIO 私有 workspaces Bucket
```

Share 和 Export 是报告交付能力；控制引用分别保存在 `shares` 与 `export_jobs`，文件统一登记为 Workspace File，ResearchState 与 Evidence 合同保持不变。

---

## 1. 共同输入与边界

输入固定为当前 Task 的一个 final report version：

```text
report.final
  → version + workspace_path + content_hash
  → 读取 reports/versions/{report_version}/report.json
  → 校验版本与 content_hash
  → 收集 ReportAst.citations[].evidence_id
  → 解析范围固定为该报告实际引用的 Evidence 和图片
```

Share 与 Export 复用 [`report_citation.md`](../02_capabilities/report_citation.md) 的 ReportAst、Citation 顺序和确定性 Renderer。Workspace 文件统一通过 [`research_workspace.md`](../02_capabilities/research_workspace.md) 的 `WorkspaceService.finalize_file` 发布。

```text
Share  → API 请求内同步冻结并发布 Snapshot
Export → ExportJob + Outbox + worker-export 异步生成文件
```

---

## 2. Share

### 2.1 冻结 Snapshot

P0 Share 只分享固定 final report：

```text
reports/shares/{share_id}/snapshot.json
reports/shares/{share_id}/assets/{asset_id}
```

`snapshot.json` 保存自包含公开投影：

```text
PublicShareSnapshot
  schema_version
  title
  report_version
  public_report
  source_cards[]
  assets[]
  created_at
```

冻结转换：

```text
ReportAst 文本 / Table
  → 公开 Report 投影

Citation.evidence_id
  → PublicSourceCard(citation_id, display_title, locator?, public_url?)

Image.workspace_path
  → 复制为 Share Asset
  → Public Report 使用 asset_id
```

`source_cards[]` 以 `citation_id` 关联公开报告中的 Citation；公开 Citation 省略内部 `evidence_id`。`evidence_id`、`doc_id`、`workspace_path` 和私有来源正文只在创建阶段解析。Web Evidence 可以生成经过 URL Policy 校验的公开 `public_url`；Document、SQL 和 Workspace Evidence 生成报告所需的标题与 locator。Share Asset manifest 保存 `asset_id + media_type + content_hash`，定位已复制文件。

### 2.2 Share 控制记录

```text
shares
  share_id
  tenant_id
  task_id
  report_version
  snapshot_workspace_path      # reports/shares/{share_id}/snapshot.json
  public_token_hash            # UNIQUE
  title                         # 省略请求 title 时取 ReportAst.title
  expires_at
  created_at
  revoked_at?
```

Share 的状态由时间字段确定：

```text
active  = revoked_at 为空且 now < expires_at
revoked = revoked_at 非空
expired = now >= expires_at
```

默认与上限：

```text
SHARE_EXPIRES_DEFAULT_DAYS=30
SHARE_EXPIRES_MAX_DAYS=90
```

### 2.3 Share API

管理接口：

```text
POST /api/v1/shares
body: {
  task_id,
  report_version?,             # 省略时在创建流程中固定当前 report.final.version
  title?,
  expires_at?
}
→ { share_id, public_url }

GET    /api/v1/shares
DELETE /api/v1/shares/{share_id}
```

公开读取：

```text
GET /api/v1/public/shares/{token}
  → PublicShareSnapshot

GET /api/v1/public/shares/{token}/assets/{asset_id}
  → Snapshot manifest 中对应的只读 Asset
```

创建成功响应包含一次性明文 Token；PostgreSQL 保存 Token hash。前端对创建动作使用 single-flight；管理列表用于查看和撤销已创建的 Share。

### 2.4 Share 创建

```text
JWT + OwnershipGuard(task)
  → 解析并固定 final report_version
  → 校验 report.final 引用与 report.json content_hash
  → 解析该 ReportAst 使用的 Citation Evidence 和图片
  → 执行公开投影与来源发布检查
  → 生成 share_id 与高熵 public token
  → 以 share_id 为稳定操作身份发布 assets 与 snapshot.json
  → PostgreSQL 事务重新锁定并校验 Task 可见
       + 写 shares(snapshot_workspace_path, token_hash, expires_at)
  → 返回 share_id + public_url
```

Share 记录在 Snapshot 与 Assets 全部 ready 后创建。对象发布后数据库提交失败产生的孤立文件由 Workspace orphan reconciliation 清理。

### 2.5 公开读取

```text
token
  → hash lookup Share
  → 校验 active
  → 读取 snapshot_workspace_path
  → 返回 PublicShareSnapshot
```

Asset 请求复用同一个 Token，访问范围等于该 Snapshot manifest 声明的 `asset_id`。Task、Document、Evidence、Workspace 目录和 Task KG 继续使用各自的私有授权入口。

---

## 3. Export

### 3.1 ExportJob

```text
export_jobs
  export_id
  tenant_id
  task_id
  report_version
  format                        # docx | pdf
  status                        # pending | running | completed | failed
  workspace_path?               # reports/exports/{export_id}.{ext}
  idempotency_key
  request_hash
  attempt_count                 # 默认 0；每次 pending → running 时递增
  error_code?
  created_at
  updated_at
  completed_at?
  expires_at?                   # completed_at + 7 days

UNIQUE(tenant_id, task_id, idempotency_key)
```

`workspace_files` 保存导出文件的 `content_hash / media_type / size / storage_ref`；ExportJob 只保存执行状态、输入版本和输出 `workspace_path`。

导出文件：

```text
reports/exports/{export_id}.docx
reports/exports/{export_id}.pdf
```

### 3.2 Export API

```text
POST /api/v1/tasks/{task_id}/report/exports
  Idempotency-Key: <client-generated-key>
  body: { format: docx | pdf, report_version?: int }
  → 202 + { export_id }

GET /api/v1/tasks/{task_id}/report/exports/{export_id}
  → status + report_version + workspace_path? + expires_at? + error_code?

GET /api/v1/tasks/{task_id}/report/exports/{export_id}/content
  → 完成且未过期时代理或签名下载 Workspace File
```

`request_hash` 表达规范化后的客户端请求语义：包含 `format`，以及 `report_version` 是显式值还是省略；`report_version` 省略本身也是稳定输入，不把随后解析出的当前 final version 回填进 `request_hash`。首次创建时将实际解析得到的 `resolved report_version` 单独写入 `export_jobs.report_version`。同一 `task_id + Idempotency-Key`、同一 `request_hash` 的重试直接返回既有 ExportJob 及其已固定版本，不因当前 `report.final` 变化而重新解析；同 key、不同 hash 返回 `409 idempotency_conflict`。

### 3.3 创建与调度

```text
JWT + OwnershipGuard(task)
  → 解析并固定 final report_version
  → 同一 PostgreSQL 事务：
       INSERT export_jobs(status=pending, attempt_count=0)
       INSERT outbox_messages(kind=report.export, payload=export_id)
  → 202 + export_id
```

Outbox 发布、Celery redelivery、队列并发和 ACK 策略由 [`deployment.md`](../04_platform/deployment.md) 定义。

### 3.4 Worker claim 与发布

```text
worker-export(export_id)
  → CAS pending → running
  → attempt_count += 1
  → 保存 expected_attempt_count
  → 读取固定 report.json 并校验 final version + content_hash
  → 解析该 ReportAst 引用的 Evidence 和图片
  → 确定性复算 Citation Validation
  → ReportRenderer.render(format)
  → WorkspaceService.finalize_file(reports/exports/{export_id}.{ext})
  → CAS WHERE status=running
              AND attempt_count=expected_attempt_count
       SET status=completed,
           workspace_path,
           completed_at,
           expires_at=completed_at+7d
  → 发布 export_updated(status=completed)
```

失败处理：

```text
可恢复失败 → CAS running → pending + 新 report.export Outbox
重试耗尽   → CAS running → failed(error_code) + export_updated(status=failed)
```

Recovery 扫描 `running` 且 `updated_at` 超过执行时限的 Job，经 CAS 重新置为 `pending` 并写入新的 Outbox。每次 claim 递增 `attempt_count`；最终 CAS 只接受当前 attempt，迟到提交得到状态冲突。

当前 attempt 在渲染和文件发布期间按相同 CAS 条件刷新 `updated_at`。相同 `report_version + format` 的渲染结果保持确定性；各 attempt 以 `export_id` 复用同一 Workspace 文件发布身份，最终只有匹配当前 `attempt_count` 的 Worker 提交 ExportJob。

### 3.5 Renderer

```text
ReportRenderer.render(report_ast, referenced_evidence, assets, format)
  → docx | pdf
  → 使用隔离临时目录
  → 读取固定报告及其引用资源
  → 返回待发布文件
```

Renderer 子进程使用固定模板和已解析 Assets，关闭外部网络访问。具体转换工具和运行镜像由 Deployment Adapter 选择。

---

## 4. 生命周期与删除

| 资源 | 可用期 | 到期或删除行为 |
|---|---|---|
| Share | 默认 30 天，最大 90 天 | 公开读取返回 404；GC 清理 Snapshot 与 Assets |
| Export | 完成后 7 天 | 下载返回 `410 export_expired`；GC 清理导出文件 |
| Task 删除 | 立即隐藏 Task | 同一事务撤销 active Share，并将 pending/running Export 标记为 `failed(task_deleted)`；Workspace 文件异步清理 |

Task soft delete、`shares.revoked_at` 和未完成 Export 的失败状态在同一 PostgreSQL 事务提交。旧 Export Worker 的 attempt CAS 随之失效；Export 通过父级 Task 授权，因此 Task 删除后立即禁止新的 Export 查询与下载授权；此前已经签发的短时签名 URL 按其短 TTL 到期。Snapshot、Assets 与导出文件随后按 Workspace GC 清理。

Share 或 Export 文件的物理删除由 `workspace_files` 与 MinIO GC 完成，控制记录保存业务状态和稳定 `workspace_path`。

---

## 5. 安全

### 5.1 Public Share

```text
高熵 Token → hash lookup
active Share → 只读 Snapshot / manifest Asset
```

- 日志、Trace 和 Audit 对 URL Token 做脱敏。
- PublicShareController 对 Snapshot 与 Asset 应用独立速率限制。
- HTML Renderer 转义原始 HTML，并执行 URL scheme policy 与 sanitizer。
- 公开响应设置 CSP、`X-Content-Type-Options=nosniff`、`Referrer-Policy=no-referrer` 和受控缓存策略。

### 5.2 Export

- 创建、查询和下载均继承父级 Task Ownership。
- Worker 输入范围固定为 final report version 及其直接引用资源。
- Export 下载按 `expires_at` 判定可用性。

---

## 6. Audit 与 Event

```text
Audit
  share.create / share.revoke
  Public Share Snapshot / Asset 读取
  export.create / export.download

Event
  export_updated(status=completed|failed)
```

Audit 保存 `share_id / asset_id? / export_id / task_id` 等资源标识；Public Share Snapshot / Asset 读取进入 Audit，但不记录明文 Token。Export Worker 的 attempt、耗时和错误进入 Trace；SSE Event 只通知客户端重新读取 ExportJob。

---

## 7. 测试与验收

```text
□ Share 输入范围固定为 final report version，省略 version 时原子固定当前 final
□ Snapshot 只包含公开 Report 投影、SourceCard 和已复制 Asset
□ Public Token 的有效读取范围等于 Snapshot 与 manifest Assets
□ Share 默认 30 天、最大 90 天；撤销、过期和 Task 删除立即使公开读取返回 404
□ Export request_hash 按客户端请求语义计算；省略 report_version 的重试复用首次固定的 resolved report_version，或在同 key 请求语义变化时返回冲突
□ Export attempt_count 隔离 Recovery 后的迟到 Worker
□ Export 输入范围等于固定 final report 及其引用 Evidence/Assets
□ Workspace File 保存文件 metadata；shares/export_jobs 保存控制状态与 workspace_path
□ DOCX、PDF 和 Share 保持 ReportAst Citation 顺序一致
□ export_updated 的 completed/failed 都能驱动前端刷新 ExportJob
□ Task 删除同步撤销 Share、立即停止新的 Export 查询与下载授权；既有短时签名 URL 按 TTL 到期；Workspace GC 异步清理文件
□ Public Renderer 的 sanitizer、CSP、Token 脱敏和 Asset scope 测试通过；Snapshot / Asset 读取进入 Audit 且不记录明文 Token
```
