# LangFuse 全链路监控使用指南

本指南说明如何为行业信息助手启用 LangFuse 全链路追踪，监控所有 LLM 调用和 Agent 协同流程。

## 监控能力

启用后，你可以在 LangFuse UI 看到：

| 维度 | 可见信息 |
|------|---------|
| **研究请求（Trace）** | 每次深度研究的完整链路、session_id、user_id、总耗时、总 token、总成本、成功/失败状态 |
| **Agent 阶段（Span）** | 6 个 Agent（architect/scout/analyst/wizard/writer/critic）的执行顺序、每阶段耗时 |
| **LLM 调用（Generation）** | 每次调用的完整 prompt、response、模型、token 用量、成本、耗时、错误信息 |
| **成本分析** | 按模型/按 Agent/按 session 的成本统计 |
| **错误聚合** | 自动聚合所有 ERROR 级别的事件，支持按错误类型筛选 |

## 快速开始

### 1. 启动 LangFuse 服务

```bash
# 进入 LangFuse 部署目录
cd docker/langfuse

# 复制环境变量模板
cp .env.example .env

# 生成必需的密钥（执行 3 次，分别填入 .env 的 NEXTAUTH_SECRET、SALT、ENCRYPTION_KEY）
openssl rand -hex 32

# 修改 .env 中的密钥和 API key（LANGFUSE_INIT_PROJECT_PUBLIC_KEY 和 SECRET_KEY）
# 这两个 key 是应用端接入 LangFuse 用的，建议改成随机字符串

# 启动 LangFuse 服务栈
docker compose up -d

# 等待 2-3 分钟，检查服务状态
docker compose ps
# 应该看到 6 个服务都是 healthy：langfuse_web、langfuse_worker、langfuse_postgres、langfuse_redis、langfuse_clickhouse、langfuse_minio
```

启动后访问 `http://localhost:3000`，用 `.env` 中配置的账号登录：
- 邮箱：`admin@industry-assistant.local`（或你修改的 LANGFUSE_INIT_USER_EMAIL）
- 密码：`admin123456`（或你修改的 LANGFUSE_INIT_USER_PASSWORD）

### 2. 配置后端应用

```bash
cd backend

# 复制环境变量模板（如果还没有 .env）
cp .env.example .env

# 编辑 .env，启用 LangFuse 并填入 API key
# LANGFUSE_ENABLED=true
# LANGFUSE_BASE_URL=http://localhost:3000
# LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx  （与 docker/langfuse/.env 中的 LANGFUSE_INIT_PROJECT_PUBLIC_KEY 一致）
# LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx  （与 docker/langfuse/.env 中的 LANGFUSE_INIT_PROJECT_SECRET_KEY 一致）

# 安装 langfuse SDK
pip install 'langfuse>=3.0.0'

# 启动后端服务
python app/app_main.py
```

启动后，日志中应看到：
```
[LangFuse] 监控客户端已初始化（SDK v3 / OpenTelemetry）
[LangFuse] Agent 'architect' 已启用 LLM 调用监控
[LangFuse] Agent 'scout' 已启用 LLM 调用监控
...
```

### 3. 配置模型价格（用于成本分析）

```bash
cd backend

# 运行模型价格配置脚本
python -m app.scripts.config_langfuse_models
```

脚本会自动配置 deepseek-v3.2、qwen-plus、qwen-max 等模型的价格。
如果价格有变动，请编辑 `app/scripts/config_langfuse_models.py` 中的 `MODEL_PRICES` 列表后重新运行。

### 4. 发起研究请求，查看 trace

在前端发起一次深度研究请求，然后打开 LangFuse UI（`http://localhost:3000`）：

1. 左侧导航点击 **Tracing**
2. 在 traces 列表中找到你的请求（按时间排序，最新的在最前）
3. 点击进入 trace 详情，可以看到：
   - 顶层是 `deep_research` trace
   - 下面是各阶段的 span：`plan_phase` → `research_phase` → `analyze_data_analyst` → `analyze_wizard` → `write_phase` → `review_phase`
   - 每个 span 下有对应的 LLM generation，包含完整 prompt、response、token、成本

## 端口分配

LangFuse 服务栈使用独立端口，与业务服务栈完全隔离：

| 服务 | 端口 | 说明 |
|------|------|------|
| LangFuse Web | 3000 | UI 和 API（对外访问） |
| LangFuse Worker | 127.0.0.1:3030 | 异步处理（仅内部） |
| LangFuse PostgreSQL | 127.0.0.1:5433 | 业务 PG 用 5432，不冲突 |
| LangFuse Redis | 127.0.0.1:6380 | 业务 Redis 用 6379，不冲突 |
| LangFuse ClickHouse | 127.0.0.1:8124 / 9002 | trace 存储 |
| LangFuse MinIO | 9090 / 127.0.0.1:9093 | 对象存储 |

## 在 LangFuse UI 查看数据

### 查看完整研究链路

1. **Tracing** 页面：所有 trace 列表，支持按 session_id、user_id、时间、状态筛选
2. 点击某个 trace 进入详情页，左侧是树状结构的 span/generation 层级
3. 每个 generation 点开后可以看到：
   - **Input**：完整的 system prompt + user prompt
   - **Output**：LLM 的完整响应
   - **Metadata**：agent 名称、json_mode、temperature 等参数
   - **Usage**：输入/输出 token 数
   - **Cost**：本次调用的成本（需配置模型价格）

### 查看成本分析

1. **Dashboard** 页面：总览统计
2. **Costs** 视图：按模型、按 Agent、按时间维度的成本分析

### 查看错误

1. **Tracing** 页面，在筛选器中选择 `Level = ERROR`
2. 可以看到所有失败的 trace/generation
3. 点击进入可以看到具体的错误信息（status_message 字段）

## 架构说明

### 插桩点

| 位置 | 文件 | 作用 |
|------|------|------|
| LangFuse 客户端封装 | `backend/app/core/langfuse_client.py` | 单例客户端、trace/span/generation 上下文管理器、优雅降级 |
| LLM 调用插桩 | `backend/app/service/deep_research_v2/agents/base.py` | `call_llm()` 方法用 `start_generation` 包裹，记录 prompt/response/token/usage |
| Agent 链路串联 | `backend/app/service/deep_research_v2/graph.py` | `_run_simplified()` 方法用 `start_trace` 包裹整个流程，每个 phase 用 `start_span` 包裹 |
| 模型价格配置 | `backend/app/scripts/config_langfuse_models.py` | 批量配置模型价格的脚本 |

### 优雅降级

LangFuse 监控是**可选的**，未启用时：
- `LANGFUSE_ENABLED=false`（默认）时，所有 LangFuse 相关操作降级为 no-op
- LangFuse 服务不可达时，SDK 会异步重试，不影响业务请求
- 业务代码不需要任何 if-else 判断，`start_trace`/`start_span`/`start_generation` 在未启用时返回空操作对象

## 常见问题

### Q: 启动后看不到 trace？

1. 检查 `.env` 中 `LANGFUSE_ENABLED=true`
2. 检查 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 是否正确
3. 检查后端日志是否有 `[LangFuse] 监控客户端已初始化`
4. 确认 LangFuse 服务已启动：`docker compose -f docker/langfuse/docker-compose.yml ps`
5. LangFuse SDK 有 2 秒的 flush 间隔，trace 可能延迟几秒出现

### Q: trace 里没有 token 和成本数据？

1. 确认 DashScope 返回了 `usage` 字段（阿里云百炼的 OpenAI 兼容接口默认返回）
2. 运行模型价格配置脚本：`python -m app.scripts.config_langfuse_models`
3. 在 LangFuse UI 的 Settings → Model Definitions 检查模型价格是否配置成功

### Q: LangFuse 服务占用太多资源？

LangFuse 的 ClickHouse 和 MinIO 会随 trace 量增长。如果只是开发测试，可以定期清理：
```bash
cd docker/langfuse
docker compose down -v  # 停止并删除数据（谨慎！）
docker compose up -d    # 重新启动
```

### Q: 如何关闭监控？

在 `backend/.env` 中设置 `LANGFUSE_ENABLED=false`，重启后端即可。不需要改代码。

### Q: LangFuse 的 UI 是英文的，看不懂？

LangFuse UI 目前是英文，但 trace 里的内容（prompt、response、agent 名称）都是中文的，会原样显示。工程使用的核心信息都在 trace 数据里，UI 文字不影响使用。
