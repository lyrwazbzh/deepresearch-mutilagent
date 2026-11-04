# 架构总览

> 目标读者：接手者。读完应能定位「改哪个功能要动哪个目录」。不写历史沿革与设计动机。
> 研究主链唯一入口：`deep_research/`（AsyncIO 六专家状态机）。

## 1. 研究主链（`POST /research/stream`）

| 项 | 说明 |
|------|----------|
| 代码 | `backend/app/service/deep_research/` |
| 状态 | `ResearchState`（TypedDict，`state.py`） |
| SSE | `core/serialization.py` → `research_router` |

## 2. 内部流程

**六类 agent**（`service/deep_research/agents/`，均继承 `BaseAgent`）按序协作：

Plan（`ChiefArchitect`）→ Research（`DeepScout`）→ Analyze（`DataAnalyst` / `CodeWizard`）→ Write（`LeadWriter`）→ Review（`CriticMaster`）→ 补搜或修订（Scout / Writer）→ Complete / QualityGateFailed

**数据通路**：`research_router` → `service.py` → `graph.py:run()` → **`_run_state_machine`**。
Agent 消息写入 `asyncio.Queue`，由状态机逐条 yield 为 SSE；并支持取消、checkpoint 与评审返工。

## 3. RAG 数据流（知识库）

```
上传文档 → DocMind 解析（service/docmind_service.py）
        → chunk_text 分块 → 向量化
        → 存入 Milvus 集合 kb_{知识库名}
检索：research/scout → service/retrieval_service.py → Milvus 相似度检索 → 注入 prompt
```

集合名转换的**权威实现在 `retrieval_service`**；注意 `knowledge_router.py` 尚有两处直接
构造集合名的存量重复（硬化候选项）。详情（分块策略、embedding、检索参数）
见 `docs/RAG架构分析.md`，本文件不复制其内容。

## 4. 基础设施依赖矩阵

| 功能 | Postgres | Redis | Milvus | MinIO | ES |
|------|:---:|:---:|:---:|:---:|:---:|
| 会话/聊天记录/检查点（SQLAlchemy） | ✅ | — | — | — | — |
| 会话缓存/限流（`core/redis_client.py`、`session_service`） | — | ✅ | — | — | — |
| 知识库向量存取（`kb_*` 集合） | — | — | ✅ | — | — |
| Milvus 自身依赖（compose 内部） | — | — | （etcd + MinIO） | — | — |
| 全文检索 | — | — | — | — | ⚠️ compose 有服务，**应用层未使用** |

> ES：`knowledge_router` / `docmind_service` 的历史注释已澄清存储实际为 Milvus（见 PR #98）。
> 聊天附件存本地 `/tmp/chat_attachments`（`attachment_router.py`），无对象存储。

## 5. 目录导航

### backend/app/

| 目录/文件 | 职责 |
|-----------|------|
| `app_main.py` | FastAPI 应用工厂 + lifespan（`create_all` 需显式 `DB_AUTO_CREATE=1`） |
| `router/` | 12 个路由模块（research/chat/knowledge/document/news/auth/session/memory/…），只做参数校验与编排调用 |
| `service/` | 业务逻辑（`deep_research/`、`retrieval_service`、`docmind_service` 等） |
| `core/` | 横切设施：config、database、redis_client、安全（upload_security）、cors、serialization |
| `models/` | SQLAlchemy ORM 模型 |
| `schemas/` | Pydantic 请求/响应模型 |
| `observability/` | OTel tracing 主链路 + LangFuse 上报（两级开关，默认后者关） |
| `config/` `scripts/` | 配置与初始化脚本（脚本的 `create_all` 属显式初始化动作） |

### frontend/src/

| 目录 | 职责 |
|------|------|
| `pages/` | 路由页面（含 `research-detail/`：研究过程可视化，关系图谱为前端基于研究结果渲染） |
| `components/` `features/` | 通用组件与功能模块 |
| `api/` | 后端 HTTP 封装 |
| `store/` | 全局状态 |
| `router/` `layout/` | 前端路由与应用框架 |
| `configs/` `utils/` `assets/` `test/` | 配置、工具、静态资源、测试 |
