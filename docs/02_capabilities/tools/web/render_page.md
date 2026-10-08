# render_page（P1）

> 动态网页渲染 fallback 的唯一合同。公共 Tool 链见 [`../../tool.md`](../../tool.md)，Web 主流程见 [`web_tools.md`](web_tools.md)，SSRF/出口策略见 [`../../../04_platform/auth_security.md`](../../../04_platform/auth_security.md)。

## 1. Tool 定位

`render_page` 使用受控无头浏览器执行页面自身 JavaScript、抽取可读正文，并像 `read_webpage` 一样归档完整文本 snapshot。它是 DeepScout Allowlist 中的独立 Tool：静态读取返回 `parse_failed`、正文显著缺失或动态内容信号后，由 DeepScout 根据 Observation 显式决定是否调用；`read_webpage` 或 ToolRuntime 不在内部自动切换到 `render_page`。

```text
✓ read_webpage 的 P1 fallback
```

## 2. 输入

```text
url: string                           # 单个绝对 http/https URL
page_index?: int = 0                  # 0-based；对抽取后正文分页
page_size_chars?: int = 12000         # 1..40000；与 read_webpage 共用限制
wait_until?: domcontentloaded | networkidle = domcontentloaded
```


## 3. 输出

与 `read_webpage` 对齐：

```text
source_uri: string
title?: string
workspace_path?: string               # 长正文外置后的 Workspace 工作记忆引用
content?: string                      # 当前输出预算内的正文页
content_hash: string
page_index: int
page_size_chars: int
has_more: bool
next_page_index?: int
retrieved_at: datetime
rendered: true
```

适合 inline budget 的完整当前页正文直接进入 ToolResult.data；长正文或明确需要文件消费的正文写入 Workspace。ToolResult 同时携带摘要与 Web 来源追溯信息。

## 4. 安全与资源控制

```text
Browser：独立、非特权浏览器沙箱；临时 profile
Network：主文档、redirect 和 subresource 请求统一经过 egress proxy + SSRFGuard
Origin：主文档使用 public http/https origin；跨域子资源使用 public 80/443
Credential：使用空白浏览器 Session
Side effects：浏览器上下文采用只读页面渲染配置
```

每次 redirect 和每个网络请求都执行地址策略；DNS 连接固定到已校验 IP。页面文本、title 与 metadata 均作为外部数据处理。

固定数值在 [`../../tool.md`](../../tool.md#12-固定限额配置) 的公共 Settings 表维护；本文消费 `RENDER_PAGE_*` 与 `DOCUMENT_PREVIEW_CHARS_*`。


## 5. 正常流程

```text
read_webpage 返回 parse_failed / dynamic-content Observation
  → DeepScout 判断是否需要动态渲染
  → DeepScout 显式调用 render_page
  → ToolRuntime 确认 P1 enabled + DeepScout allowlist
  → SSRFGuard 主 URL
  → 启动一次性受控 Browser Context
  → 受控加载并等待 domcontentloaded/networkidle（受总 timeout 限制）
  → Readability/DOM 文本抽取与 Normalize
  → 计算 content_hash
  → 当前预算内正文作为完整 data 返回
  → 长正文或后续文件输入通过 WorkspaceService.finalize_file 外置
  → 返回正文或 workspace_path
  → 销毁 Browser Context
```

逻辑读取的幂等键为 `sha256(tenant_id|task_id|node_run_id|render_page:{canonical_url}:page:{page_index}:{renderer_profile_version})`。同一 URL 的不同正文页是不同逻辑读取；相同 URL、`page_index`、renderer profile 与 NodeRun 的重放复用原 ToolResult。长正文外置时，`content_hash` 标识对应的 Workspace snapshot。新的逻辑节点执行在页面内容变化时产生新的 ToolResult，并在需要外置时产生新的 snapshot。

## 6. 错误与降级

| 情况 | ToolError |
|---|---|
| URL/分页参数非法 | `invalid_argument` |
| SSRF/出口策略拦截 | `policy_denied` |
| 401/403/验证码/登录墙 | `policy_denied`（code=`source_access_denied`） |
| timeout | `timeout`；只读，可按 Runtime 策略有限 retry |
| 浏览器崩溃 | `tool_error`（code=`browser_crash`） |
| 页面/DOM/响应超限 | `limit_exceeded` |
| 抽取后仍为空 | `tool_error`（code=`parse_failed`） |
| 用户取消 | `cancelled` |


## 7. 权限、副作用与可观测

- 仅 DeepScout；P1 feature flag 开启后可用。
- 长正文写入当前 Task Workspace；Graph Node 将引用写入 research_rounds。
- Tool 专用属性：`requested_host/final_host`, `wait_until`, `subrequest_count`, `decoded_bytes`, `dom_nodes`, `returned_chars`, `error.type/code`；耗时使用 Span duration，结果引用留在 ToolResult/Evidence。

## 8. 验收标准

```text
□ read_webpage 只返回失败/动态内容 Observation，由 DeepScout 显式调用 render_page；ToolRuntime 不自动嵌套 Tool
□ 主请求、redirect 与 subresource 全部经过出口策略
□ 浏览器运行在非特权临时容器，网络统一经过 proxy
□ 60s/100 requests/20MiB/200k DOM nodes/3 redirects 硬上限生效
□ 输出与 read_webpage 的短结果/长结果合同一致
□ Workspace finalize_file 保障原子发布
□ 稳定语义槽包含 canonical_url + page_index + renderer_profile_version；同一逻辑读取重放复用 ToolResult 与对应 snapshot
```
