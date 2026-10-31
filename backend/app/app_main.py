# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import logging
from observability import (
    ObservabilityMiddleware,
    configure_logging,
    initialize_tracing,
    metrics_response,
    shutdown_tracing,
)

# 加载环境变量
load_dotenv()

# 配置日志
configure_logging()
logger = logging.getLogger(__name__)

from router import document_router, search_router, chat_router, research_router
from router.observability_router import router as observability_router
from router.auth_router import router as auth_router
from router.session_router import router as session_router
from router.knowledge_router import router as knowledge_router
from router.attachment_router import router as attachment_router
from router.memory_router import router as memory_router
from router.database_router import router as database_router
from router.news_router import router as news_router
from router.reliability_router import router as reliability_router
from router.research_memory_router import router as research_memory_router
from core.cors import build_cors_kwargs
from core.security_headers import security_headers_for
from core.database import engine, Base
# 导入所有模型以确保它们被注册
from models import (
    User, ChatSession, ChatMessage, ChatAttachment, LongTermMemory,
    KnowledgeBase, Document, IndustryStats, CompanyData, PolicyData,
    ResearchCheckpoint, IndustryNews, BiddingInfo, NewsCollectionTask,
    ResearchEvent, ResearchRun,
    ToolExecution, DeadLetterTask, SearchIndexOutbox,
    FactMemory, ExperienceMemory,
)

# 建表以 backend/migrations/ 的手写迁移 SQL 为准；create_all 默认**不执行**，
# 避免 ORM 模型与迁移 SQL 两套 schema 来源漂移（tickets.md T12 / PRD NG-7）。
# 仅在显式设置 DB_AUTO_CREATE=1（本地开发自用）时才自动建表。
if os.getenv("DB_AUTO_CREATE") == "1":
    Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and close application-level background services."""
    initialize_tracing()
    """应用生命周期管理"""
    # 启动时执行
    logger.info("应用启动中...")

    # 安全配置校验：core.security 在导入期校验 JWT 密钥，缺失 / 过弱会抛 RuntimeError 终止启动。
    # 这里显式导入一次，保证该校验属于「启动路径」的一部分，而不依赖路由模块的导入顺序。
    from core.security import SECRET_KEY  # noqa: F401

    logger.info("JWT 密钥校验通过（长度 %d）", len(SECRET_KEY))

    # 初始化定时任务调度器并检查数据
    try:
        from service.scheduler_service import init_scheduler_and_check_data
        await init_scheduler_and_check_data()
        logger.info("定时任务调度器启动成功")
    except Exception as e:
        logger.error(f"定时任务调度器启动失败: {e}")

    yield

    # 关闭时执行
    logger.info("应用关闭中...")
    try:
        from service.scheduler_service import get_scheduler_service
        scheduler = get_scheduler_service()
        scheduler.stop()
    except Exception as e:
        logger.error(f"定时任务调度器关闭失败: {e}")
    shutdown_tracing(timeout_seconds=5.0)


# 版本号单一来源 = app/__init__.py 的 __version__（T25 初始化，CHANGELOG 0.1.0 同源），不再硬编码。
# app_main 有三种导入方式，故两种路径都试：
#   ① 包内导入（pytest：`from app import app_main`）；
#   ② 脚本 / 容器顶层导入（`python app/app_main.py`、`uvicorn app_main:app`），此时包目录自身在 sys.path 上。
try:
    from app import __version__ as _APP_VERSION
except ImportError:
    from __init__ import __version__ as _APP_VERSION

app = FastAPI(
    title="行业信息助手 API",
    description="基于 AI Agent 的行业信息助手系统",
    version=_APP_VERSION,
    lifespan=lifespan
)

# 添加 CORS 中间件
# 来源白名单从 CORS_ALLOW_ORIGINS 读取（逗号分隔）；含通配时强制 allow_credentials=False，
# 生产环境留空则直接在启动阶段失败。解析与判定逻辑见 core/cors.py（独立成模块以便无基础设施单测）。
app.add_middleware(CORSMiddleware, **build_cors_kwargs())
app.add_middleware(ObservabilityMiddleware)

# 安全响应头（T37 方案 A）：为响应附加 CSP。
# 策略值与文档路径豁免规则见 core/security_headers.py（独立成模块以便无基础设施单测）。
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in security_headers_for(request.url.path).items():
        response.headers[name] = value
    return response

# 注册路由
app.include_router(auth_router)
app.include_router(session_router)
app.include_router(knowledge_router)
app.include_router(attachment_router)
app.include_router(memory_router)
app.include_router(database_router)
app.include_router(document_router)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(research_router)
app.include_router(observability_router)
app.include_router(news_router)
app.include_router(reliability_router)
app.include_router(research_memory_router)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Expose application metrics for Prometheus scraping."""

    return metrics_response()

@app.get("/hello")
async def hello_world():
    """
    Simple hello world endpoint for network verification
    """
    return {
        "status": "success",
        "message": "Hello World! The API is working correctly."
    }

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
