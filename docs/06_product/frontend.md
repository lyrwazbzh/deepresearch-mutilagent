# DeepResearch-mutilagent：研究工作台

## 0. 定位

前端是 Thread、ResearchTask 及其研究产物的交互层，只消费公开 HTTP / SSE 合同。

```text
左侧 → 产品导航
中间 → 当前 Thread 的对话、Task Progress 与 HITL
右侧 → 当前 Task 的研究结果面板
```

```text
Frontend
  ↕ api_lifecycle.md       # Task / Thread / SSE / Evidence / Workspace / Document API
  ↕ report_citation.md     # ReportAst、Citation 展示与来源跳转
  ↕ share_export.md        # final report 的 Share / Export
```

对应合同见 [`api_lifecycle.md`](../04_platform/api_lifecycle.md)、[`report_citation.md`](../02_capabilities/report_citation.md) 和 [`share_export.md`](share_export.md)。Graph、Agent、ResearchState 持久化和物理存储由后端实现；前端以 TaskProjection、资源 API 和 Event 为唯一运行时合同。

---

## 1. 产品概念与页面状态

```text
Thread       = 一段产品对话，可包含 0..N 个 ResearchTask
ResearchTask = Thread 下的一次研究执行
Document     = 产品“知识库”中的持久文件
Evidence     = 当前 Task 的统一证据卡片
Workspace    = 当前 Task 的长内容与生成文件
```

登录会话使用 Auth Session；产品对话统一称为 Thread。

前端本地状态只保存：

```text
auth_context?             # 内存中的 Access Token + 最小 user / tenant / roles 投影
selected_thread_id
selected_task_id?
active_tab
resource_cache
pending_command?
last_event_id?
```

Task 生命周期、PendingWrite、报告版本和 ExportJob 状态每次都以服务端投影为准。

### 1.1 Auth Session 与登录恢复

前端必须完成最小 Auth 产品闭环：

```text
未登录
  → Login
  → POST /auth/login
  → 获得 Access Token
  → Refresh Credential 由 HttpOnly Cookie 保存
  → 进入产品工作区
```

Access Token 只保存在当前前端运行时，不持久化 Refresh Credential。Refresh Credential 由浏览器 Cookie 管理，前端 JavaScript 不读取其内容。

普通 API 请求：

```text
HTTP Request
  → Authorization: Bearer <access_token>
  → 成功则正常返回

普通 API 请求返回 401
  → 单次 POST /auth/refresh
  → refresh 成功
       → 更新 Access Token
       → 重放原请求一次
  → refresh 失败
       → 清理当前前端登录态
       → 返回 Login

POST /auth/refresh 自身返回 401
  → 不再次调用 refresh
  → 清理当前前端登录态
  → 返回 Login
```

多个请求同时收到 `401` 时，只允许一个 refresh 请求执行；其他请求等待同一 refresh 结果，避免并发 Refresh Session rotation。

用户主动退出：

```text
POST /auth/logout
  → 服务端撤销 Session 并清理 Refresh Cookie
  → 前端清理 Access Token 与用户投影
  → 返回 Login
```

前端只消费服务端返回的最小 user / tenant / roles 投影，不自行推导权限。

---

## 2. 布局与导航

### 2.1 左侧导航

| 导航项 | 职责 |
|---|---|
| 首页 | 产品入口与说明 |
| 我的聊天 | 返回当前 Thread 工作区 |
| 对话历史 | `GET /threads`，选择已有 Thread |
| 知识库 | `GET /documents`，浏览、上传和管理 Document ingestion |
| 数据源 | 浏览当前可用 DataSource；管理员维护连接、Policy 与 Grant |
| 系统设置 | ModelConfig 管理与模型连接测试 |

“我的聊天”和“对话历史”使用同一种 Thread 资源：前者回到当前工作区，后者负责选择历史 Thread。

### 2.2 工作区布局

```text
尚未选择 ResearchTask
┌──────────┬───────────────────────────────────┐
│ 产品导航 │ 当前 Thread 对话与输入             │
└──────────┴───────────────────────────────────┘

已选择 ResearchTask
┌──────────┬───────────────────────┬─────────────────────────────┐
│ 产品导航 │ 对话 / Progress / HITL │ 当前 Task 的研究结果面板      │
└──────────┴───────────────────────┴─────────────────────────────┘
```

右侧面板固定为四个 Tab 和一块 Content：

```text
搜索结果 | 知识图谱 | 可视化 | 研究报告
```

`active_tab` 只决定 Content 显示内容，不改变任何服务端状态。

---

## 3. Thread 与 Task 选择

路由保存当前选择：

```text
thread_id + task_id
  → 加载 Thread messages
  → 加载 TaskProjection
  → 加载当前 Tab 资源
  → 为非终态 Task 建立 SSE
```

只有 `thread_id` 时：

```text
GET /threads/{thread_id}/messages
+ GET /tasks?thread_id={thread_id}
  → 默认选择 updated_at 最新的 Task
  → 用户切换 Task 时把 task_id 写回路由
```

新建研究只调用一次 `POST /tasks`。该请求同时持久化本次用户消息并返回 `thread_id + task_id`；前端随后切换到对应路由。

输入区统一提交到 `POST /tasks`。前端不要求用户预先选择“聊天”或“研究”；服务端 RouteNode 决定 `direct | focused | deep`：

```text
用户输入 → POST /tasks
  direct  → ChiefArchitect 无工具回答
  focused → 专业执行 Agent 完成有界工具任务
  deep    → 完整研究 DAG
```

运行中的 Task 可通过 `POST /tasks/{task_id}/replan` 提交改目标指令。

---

## 4. 中间区域：对话、Progress 与控制

### 4.1 对话

中间区域按 `GET /threads/{thread_id}/messages` 回放持久化消息。ResearchTask 的阶段、Evidence 或报告更新通过研究卡片呈现，不伪装成新的对话消息。

### 4.2 Progress

Progress 读取 TaskProjection：

```text
task_status → 生命周期文案与控制入口
phase       → 当前研究阶段文案
```

展示规则：

| 服务端投影 | UI |
|---|---|
| `pending` | 排队中 |
| `running` | 按 `phase` 显示规划、检索、写作或审阅 |
| `pausing` | 正在暂停 |
| `paused` + `pending_write` | 等待确认数据库变更 |
| `paused` | 已暂停 |
| `cancelling` | 正在取消 |
| `cancelled / completed / failed` | 终态摘要；保留已有产物 |

控制按钮调用 [`api_lifecycle.md`](../04_platform/api_lifecycle.md) 定义的 `pause / resume / cancel / replan`。命令返回 `202` 后，前端记录 `pending_command` 并等待新的 TaskProjection，不提前改写服务端状态。

---

## 5. 右侧资源适配器

四个 Tab 的可用性由对应资源决定：

| Tab | 主入口 | Content |
|---|---|---|
| 搜索结果 | `GET /tasks/{task_id}/evidence` | Web、Document、SQL、Compute Evidence 卡片 |
| 知识图谱 | `POST /tasks/{task_id}/knowledge-graph/query` | Entity、Claim、Relation 与 `evidence_id` 来源 |
| 可视化 | `GET /tasks/{task_id}/workspace?prefix=visualizations/` | Workspace 图表和表格文件 |
| 研究报告 | TaskProjection 的 `report.draft / report.final` | 按 `workspace_path` 读取 ReportAst 并渲染 |

每个 Tab 独立使用：

```text
idle | loading | ready | empty | error
```

Tab 位置保持固定。任务有新资源时更新角标或状态；用户已经选择的 Tab 保持不变。

### 5.1 Evidence 来源打开方式

| Evidence 内容位置 | UI 行为 |
|---|---|
| 内联短结果 `data` | 在 Evidence 详情中直接展示 |
| `workspace_path` | `GET /tasks/{task_id}/workspace/{workspace_path}` |
| `doc_id + chunk_id` | 展示 Evidence 摘要与 locator；通过 `/documents/{doc_id}/content` 打开知识库原文件 |
| Web `source_uri` | 在来源卡片显示 URL 与 `retrieved_at` |

Evidence API 是来源发现入口；Workspace 和 Document API 负责读取引用指向的内容。

### 5.2 Report 与 Citation

前端使用 `report.draft` 或 `report.final` 的 `workspace_path + content_hash` 读取固定版本的 `report.json`，再通过统一 ReportAst Renderer 展示正文、表格、图表和 Citation。

`report.final` 存在时展示 final；研究仍在进行且只有 `report.draft` 时展示最新 draft，并明确标记“草稿”。

`route.mode=direct|focused` 的任务完成后不生成 ReportAst；可见结果作为 assistant Message 写入 Thread。收到 `task_updated.changed` 包含 `messages` 时刷新消息区。`route.mode=deep` 才启用完整 Evidence / 报告工作流。

```text
Citation.evidence_id
  → GET /tasks/{task_id}/evidence/{evidence_id}
  → 根据 Evidence 引用打开内联内容、Workspace 文件或 Document 原文
```

前端只负责显示与跳转；Citation 有效性由报告管线完成。

---

## 6. SSE 与资源同步

SSE 只通知已提交资源发生变化：

```text
task_updated
  task_status / phase / route → 刷新 TaskProjection
  messages             → 刷新 Thread messages
  evidence             → 刷新 Evidence 列表
  knowledge_graph      → 失效当前 Task KG 查询缓存
  report               → 刷新 TaskProjection 并读取新报告版本
  pending_write        → 刷新 TaskProjection 与 PendingWrite

export_updated（Phase 14 预留）
  → GET /tasks/{task_id}/report/exports/{export_id}
```

页面初次进入和每次 SSE 重连都执行 Task snapshot reconcile。客户端保存最近的 `event_id`，并通过 `Last-Event-ID` 续传：

```text
SSE 建连 / 重连返回 HTTP 409 event_cursor_expired
  → 此时尚未建立 SSE stream
  → 丢弃过旧 cursor
  → 重新加载 TaskProjection 与当前 Tab 资源
  → 建立新的 live stream

已连接 SSE 收到 resync_required transport control
  → 当前连接随后结束
  → 重新加载 TaskProjection 与当前 Tab 资源
  → 建立新的 live stream
```

终态 Task 完成最后一次 snapshot reconcile 后关闭 SSE；静态产物继续通过资源 API 读取。

---

## 7. HITL：数据库写审批卡

审批内容与状态合同见 [`database_write.md`](../02_capabilities/database_write.md)。

### 7.1 加载

```text
TaskProjection.task_status = paused
+ pending_write = write_id
  → GET /tasks/{task_id}/pending-writes/{write_id}
  → 展示 PendingWrite.preview
```

审批卡展示：

```text
datasource
operation
table
filters?
values?
reason
current_version?
expected_value?
```

### 7.2 决策

```text
批准
POST /tasks/{task_id}/pending-writes/{write_id}/approve
body: { plan_hash, comment? }

驳回
POST /tasks/{task_id}/pending-writes/{write_id}/reject
body: { reason? }
```

`plan_hash` 取自本次 Preview 响应，使批准动作绑定用户实际看到的计划。Approve 与 Reject 均携带 `Idempotency-Key`。

命令被受理后，卡片进入“提交中”状态；`task_updated` 到达后重新读取 TaskProjection 和 PendingWrite。`stale_write_plan`、`plan_hash_mismatch` 或 `expired` 显示对应结果，并将原卡片保留为只读记录。

协作暂停的 Resume 与 PendingWrite 的 Approve 是两个独立操作：

```text
paused + pending_write → Approve / Reject / Cancel
paused                → Resume / Cancel
```

---

## 8. 两类上传

### 8.1 知识库 Document

知识库页面处理可跨 Task 检索的文件：

```text
POST /documents
  → PDF / DOCX / TXT / MD / HTML
  → 显示 Document status 与 ingestion stage
  → 支持 retry / rebuild / delete
```

### 8.2 Task 数据文件

当前 ResearchTask 的输入区提供“添加数据文件”：

```text
POST /tasks/{task_id}/data-files
  → XLSX / CSV / Parquet
  → 返回 workspace_path
  → 作为当前 Task 的 SQL/Data 输入
```

上传进度、大小或格式错误在当前输入区显示；已发布文件显示文件名、大小和就绪状态，组件内部保存返回的 `workspace_path`。Document 与 Task 数据文件分别使用各自的 API、生命周期和页面位置。

---

## 9. DataSource 管理

DataSource 页面用于管理 Agent 可访问的外部 PostgreSQL 业务数据库，不承担 BI 查询或数据分析展示。P0 只支持 PostgreSQL，不提供多数据库类型选择或通用驱动配置界面。

普通用户：

```text
GET /datasources
  → 只显示当前用户被授权的数据源最小投影
  → 展示 datasource_id / name / enabled / write_ready
  → 不展示连接元数据、Credential、完整 Policy 或 Grant
```

管理员可以执行：

```text
POST   /datasources
PATCH  /datasources/{datasource_id}

POST   /datasources/{datasource_id}/enable
POST   /datasources/{datasource_id}/disable

PUT    /datasources/{datasource_id}/policies
PUT    /datasources/{datasource_id}/grants/{user_id}
DELETE /datasources/{datasource_id}/grants/{user_id}
```

管理员页面展示：

```text
datasource_id
name
enabled
write_ready
PostgreSQL connection metadata    # host / port / database_name / ssl_mode 等非敏感字段
Policy 摘要
Grant 摘要
```

Credential 明文不回显。前端只提交新增或轮换凭据所需输入，已保存 Secret 继续由后端 SecretsService 管理。

普通用户不能获得管理员连接配置、Credential 或完整 Policy 管理能力；按钮是否可见依据服务端 roles / API 权限投影，不在前端重新建立权限系统。

---

## 10. 系统设置：ModelConfig

系统设置首期只承载模型配置管理，不扩展为通用 SaaS 后台。

使用 Phase 10 已公开的 ModelConfig API：

```text
GET    /model-configs
POST   /model-configs
GET    /model-configs/{model_config_id}/versions/{version}
PATCH  /model-configs/{model_config_id}

POST   /model-configs/{model_config_id}/test
POST   /model-configs/{model_config_id}/disable
POST   /model-configs/{model_config_id}/versions/{version}/revoke
```

页面支持：

```text
模型配置列表
  → 查看 provider / model / enabled 等公开投影

创建 / 编辑
  → 提交模型配置

Test
  → 调用服务端连接测试
  → 展示成功或安全错误信息

Disable
  → 停用配置

Version
  → 查看已有版本
  → revoke 指定版本
```

API Key、Provider Secret 等敏感值不回显；前端只在创建或轮换时提交新值。

Task 使用哪个模型、Role Binding 如何解析以及 Task 创建后的 frozen Model Binding 均由后端负责。前端不维护第二套 Agent → Model Binding 状态。

---

## 11. Share 与 Export（Phase 14 预留）

Share / Export 的完整业务实现属于 Phase 14，合同见 [`share_export.md`](share_export.md)。

Phase 11 不实现 Share 创建或撤销、Export 创建或下载，也不把这些能力纳入本阶段验收。前端保留 `export_updated` 的事件适配边界，供 Phase 14 接入时复用；当前研究工作台的完成标准截至 final report 展示与 Citation 跳转。

---

## 12. 客户端命令与幂等

前端为公开 API 要求幂等的写操作生成 `Idempotency-Key`：

```text
CreateTask
pause / resume / cancel / replan
PendingWrite approve / reject
Document upload / Task data-file upload
Export create（Phase 14）
```

一次用户动作对应一个 key；同一动作的网络重试复用该 key；用户重新发起动作时生成新 key。组件在请求完成前使用单次提交状态，并以服务端响应和后续投影结束该状态。

---

## 13. 加载、错误与渲染

所有资源适配器统一处理：

```text
loading              → 骨架或进度
empty                → 资源专属空态
401                  → 进入登录恢复
404                  → 资源不可见或已删除
409 invalid_state    → 刷新 TaskProjection
409 event_cursor_expired → 丢弃过旧 cursor，snapshot reconcile 后重新建立 SSE
SSE resync_required      → 当前连接结束，snapshot reconcile 后重新建立 SSE
422 / 413            → 显示字段或文件错误
5xx / network error  → 保留当前内容并提供重试
```

ReportAst、Markdown、Evidence 文本和知识图谱标签使用统一的转义与净化 Renderer；外部 URL 使用受控 scheme，Workspace 与 Document 二进制内容按服务端 `Content-Type` 预览或下载。

---

## 14. 验收标准

```text
□ Thread、Task 与 Auth Session 术语清晰
□ Login → Access Token → Refresh → Logout 的 Auth 前端闭环可用
□ 路由固定 selected_thread_id / selected_task_id，历史恢复结果确定
□ 三栏研究态与四个固定 Tab 正常工作
□ Tab 由资源 API 驱动，phase 只影响 Progress 文案
□ task_updated 只负责失效与刷新资源
□ 所有输入统一通过 POST /tasks；direct / focused 结果读取 assistant Message，deep 结果读取 ReportAst
□ SSE 建连返回 409 event_cursor_expired 与已连接流收到 resync_required 时，分别按各自入口完成 snapshot reconcile 并重新连接
□ Evidence 能打开内联、Workspace、Document 和 Web 来源
□ PendingWrite Preview 跨刷新恢复，Approve 提交 plan_hash
□ 命令请求按一次用户动作复用 Idempotency-Key
□ 知识库 Document 与 Task XLSX/CSV/Parquet 上传入口分离
□ DataSource 页面明确区分普通用户最小投影与管理员字段/操作；P0 只支持 PostgreSQL，Credential 不回显
□ ModelConfig 支持列表、创建/编辑、test、disable 与 version revoke，Secret 不回显
□ final report 正确渲染，并支持 Citation → Evidence → Source 跳转
```

## 附录：最小测试集

```text
□ 创建 Task → SSE → Evidence → final report
□ Access Token 失效后单次 refresh 成功并重放原请求；refresh 自身 401 不递归刷新并返回 Login
□ 同一 Thread 切换多个 Task 后路由、资源与 SSE 不串线
□ SSE 普通断线重连、建连时 HTTP 409 event_cursor_expired、连接中 resync_required 三条路径均恢复一致快照
□ PendingWrite 刷新恢复、plan_hash 审批与重复点击幂等
□ Document 上传与 Task data-file 上传进入不同资源路径
□ 普通用户不能进入 DataSource 管理操作或读取连接元数据，管理员可完成 PostgreSQL 配置与 Policy / Grant 管理
□ ModelConfig 创建 → test → disable / revoke 的前端路径正常
□ Web/SQL 长内容、Document 来源、Workspace 图表和 Citation 均可回读
```
