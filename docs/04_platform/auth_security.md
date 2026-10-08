# DeepResearch-mutilagent：认证与安全边界

## 0. 文档职责

本文定义四件事：用户认证、资源授权入口、凭据保护和外部网络访问安全。

```text
Authentication  → 当前请求者是谁
Authorization   → 当前请求者能否对具体资源执行该动作
Secret handling → 经授权的 Adapter 如何按需取得凭据
Network policy  → 外部 HTTP、MCP 与数据库连接可以访问哪里
```

Document 可读范围见 [`auth_tenant.md`](auth_tenant.md)；Research LTM 召回规则见 [`memory.md`](../02_capabilities/memory.md)；Document 的 RRF、Rerank 与 provenance 见 [`rag.md`](../02_capabilities/rag.md)。

---

## 1. 认证

### 1.1 密码与会话

- 密码使用 Argon2id 哈希；数据库只保存哈希与参数。
- Access Token 默认 15 分钟，仅用于 API 鉴权。
- Refresh Session 默认 30 天，服务端保存 `session_id`、refresh secret 哈希、用户、租户、过期时间与撤销状态。
- Refresh Token 使用 `session_id.secret`；每次刷新轮换 secret 并使旧 secret 失效。
- 登出、管理员撤销或用户退出租户时撤销对应 Refresh Session；已签发 Access Token 依靠短 TTL 到期。

Refresh Session 的 secret 校验与轮换必须原子执行：

```text
session_id 定位 Session
  → 校验未撤销、未过期
  → 校验当前 refresh secret hash
  → 通过 CAS 或行锁完成 secret rotation
  → commit
  → 签发新的 Access Token 与 Refresh Credential

同一旧 secret 在成功轮换后再次使用
  → 401 invalid_refresh_session
  → 不签发新的 Access Token 或 Refresh Credential
```

两个请求并发使用同一个旧 secret 时只有一个轮换成功；另一个请求按旧 secret replay 处理，不增加 token family 或第二套会话模型。

### 1.2 Refresh Cookie 与跨站防护

```text
Refresh Cookie
  HttpOnly
  Secure
  SameSite=Lax              # 默认同站部署
  Path=/api/v1/auth
  不向 JavaScript 暴露
```

`POST /auth/refresh` 与 `POST /auth/logout` 使用 Cookie Credential，执行前必须校验 `Origin` 属于配置的前端 Origin。若未来前后端必须跨站部署并使用 `SameSite=None`，Cookie 仍必须设置 `Secure`，同时启用显式 CSRF Token 校验；仅使用 HttpOnly Cookie 不能替代跨站请求防护。

### 1.3 Access Token 合同

JOSE Header：

```yaml
alg: configured_asymmetric_algorithm
kid: signing_key_id
typ: JWT
```

Claims：

```yaml
sub: user_id
tenant_id: tenant_id
roles: [member | admin]
iss: configured_issuer
aud: configured_audience
iat: issued_at
exp: expires_at
```

API Middleware 固定允许的签名算法，并校验签名、`kid`、`iss`、`aud`、`iat`、`exp` 和允许的时钟偏差。验证后只向业务层提供：

```text
AuthContext(tenant_id, user_id, roles[])
```

`task_id`、`thread_id`、`doc_id`、`datasource_id` 等资源标识来自路由或服务端记录，不进入 JWT。切换租户时重新校验有效成员关系，并签发该租户下的新 Token。

---

## 2. 逐请求授权

授权输入是 `AuthContext + action + 具体资源标识`。API 先按路由中的具体标识加载授权根，再检查 Task/Thread owner、Document tenant membership 与操作角色、DataSource Grant 或公开 Share 规则。

例如：

```text
GET /tasks/task_123
  → JWT → AuthContext(tenant=A, user=U1)
  → SELECT ... FROM research_tasks
       WHERE tenant_id=A AND user_id=U1 AND task_id=task_123
  → 返回 Task 投影
```

子资源继承其授权根：Message 继承 Thread，Evidence、Workspace File、Task KG、PendingWrite 与 ExportJob 继承 ResearchTask，Document Chunk 与原始对象继承 Document。完整层次见 [`auth_tenant.md`](auth_tenant.md)。

资源不存在与无权访问对外统一返回 `404`；已认证但不具备某项管理动作的角色权限返回 `403`。每次请求都重新执行对象级授权，不依赖前端隐藏或先前请求结果。

---

## 3. 凭据保护与按需解析

模型、MCP 与 DataSource 凭据使用同一原则：

```text
数据库记录保存 secret_ref
Secret Store 保存经认证加密的 ciphertext + key_id
MASTER_KEY / root key 由部署环境提供
授权与策略校验通过
  → 对应 Adapter 按本次调用 JIT 解析并解密
  → 仅在连接或请求生命周期内使用
```

明文凭据不进入 JWT、AuthContext、Task ExecutionContext、ResearchState、Tool 参数、日志、Trace、Event 或错误响应。轮换时创建新 secret revision，验证通过后切换引用；旧 revision 按保留策略撤销和清理。

DataSource 的具体执行顺序见 [`datasource.md`](../02_capabilities/datasource.md)。

---

## 4. HTTP / Web / MCP 网络安全

所有服务端主动 HTTP 请求共用出站策略：

```text
解析 URL 与规范化 host
  → 校验 scheme / port / endpoint policy
  → 同时解析 A 与 AAAA
  → 所有候选 IP 均满足出站规则
  → 将连接固定到已校验地址
  → 限制连接、读取超时与响应体大小
  → 每次 redirect 重新执行完整校验
```

Web 访问默认只开放 `http/https` 的允许端口。需要访问私网的 MCP Endpoint 由运维配置显式 egress allowlist；随后按 `endpoint policy/SSRF check → JIT credential → handshake → tools/list` 建立连接。DNS rebinding、IPv4/IPv6 私网、回环、链路本地和云元数据地址均由出站策略处理。

数据库协议不复用 HTTP SSRF 校验器。DataSource 连接依据租户级数据库 egress policy / allowlist 校验主机、端口与驱动，再建立受控连接池。

---

## 5. 分层边界

| 层 | 决策 |
|---|---|
| Authentication Middleware | Token 是否有效，构造最小 `AuthContext` |
| Ownership / Access Guard | 当前用户是否可对具体资源执行动作 |
| Tool allowlist / policy | 当前 Agent、节点与任务是否可以调用该能力 |
| Adapter | 授权通过后解析 `secret_ref`，执行外部调用 |
| PendingWrite approval | 业务数据库写操作是否已经获得人类批准 |

这些决策串联执行，但互不替代。业务写批准不扩大 DataSource Grant，Tool allowlist 也不授予 Document 或 Task 的读取权限。

---

## 6. 公开 Share

公开 Share 是私有资源授权之外的只读入口：

```text
opaque share token
  → hash lookup
  → 校验 revoked_at / expires_at
  → 读取创建 Share 时冻结的 Workspace Snapshot
  → Asset 请求限制在 Snapshot manifest 声明的 asset_id
```

Share Token 表达单个 Snapshot 及其 manifest Assets 的读取授权。Task、Document、Evidence、Workspace 目录和实时 Task KG 继续使用私有资源授权；速率限制、下载审计和响应安全头独立应用。

---

## 7. 验收标准

```text
□ JWT 只携带身份、租户、角色和标准时效/签发声明
□ Refresh secret 校验与轮换原子执行；同一旧 secret 并发或重放时只有一次成功，其余返回 401
□ Refresh Cookie 使用 HttpOnly、Secure、SameSite 与受限 Path；refresh/logout 校验 Origin，跨站模式额外启用 CSRF Token
□ 每个私有资源请求执行对象级授权；Task 标识不从 JWT 推导
□ 凭据只在授权后的 Adapter 调用中按需解析，明文不进入状态与观测数据
□ HTTP/MCP 对 A、AAAA、连接地址与每次 redirect 执行出站校验
□ DataSource 使用数据库 egress policy，并在执行时重新校验 Grant 与 Policy
□ 公开 Share Token 的有效范围等于冻结 Snapshot 与 manifest Assets
```

## 8. 实现顺序

1. Argon2id、Refresh Session 原子轮换、Refresh Cookie/CSRF 防护、JWT signer/verifier 与 `AuthContext`。
2. 按授权根实现 Task、Thread、Document、DataSource、ModelConfig 与 Share Guard。
3. Secret Store、revision/rotation 与 Adapter JIT resolver。
4. HTTP/MCP egress policy、DNS/redirect 测试与数据库 egress policy。
5. 对象级越权、跨租户、日志脱敏和公开 Share 集成测试。
