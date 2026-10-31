# 架构总览

> 目标读者：接手者。读完应能定位「改哪个功能要动哪个目录」。不写历史沿革与设计动机。
> V2 是默认研究主链；V1 ReAct 仅用于显式兼容请求，不参与 V2 内部编排。

## 1. 两条研究路线（均由 `POST /research/stream` 进入，`version` 字段选择）

| 路线 | 代码位置 | 触发 | 状态管理 | 现状 |
|------|----------|------|----------|------|
| **V2 多智能体**（默认） | `backend/app/service/deep_research_v2/` | `version=v2` | `ResearchState`（dataclass，`state.py`） | 日常主路径 |
| **V1 ReAct** | `service/dr_g.py` + `react_controller.py` + `tool_executor.py` | `version=v1` | ReAct 循环内的消息列表 | **有意保留（NG-3）**，日常主路径不可达但不是死代码 |

两条路线的 SSE 事件均由 `core/serialization.py:serialize_event` 序列化后经 `research_router` 推给前端。

## 2. V2 内部流程

**六类 agent**（`service/deep_research_v2/agents/`，均继承 `BaseAgent`）按序协作：

Plan（`ChiefArchitect`）→ Research（`DeepScout`）→ Analyze（`DataAnalyst`）→ Write（`LeadWriter`）→ Review（`CriticMaster`）→（Review 不通过时 Revise：`CodeWizard`）→ Complete

**数据通路**：`research_router` → `service.py` → `graph.py:run()`。实际执行的是
**手写异步状态机 `_run_state_machine`**（支持实时 SSE 流式）；agent 产出的消息写入
`asyncio.Queue`，由 `_run_state_machine` 逐条取出并 yield 为 SSE 事件（`run()` 仅委托转发）。

`graph.py` 仅维护支持逐消息 SSE、取消、checkpoint 与评审返工的 AsyncIO 状态机。

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
| `service/` | 业务逻辑（`deep_research_v2/`、`dr_g.py`、`retrieval_service`、`docmind_service` 等） |
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
