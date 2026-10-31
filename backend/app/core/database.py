# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""数据库连接和会话管理"""
import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


def _require_env(name: str) -> str:
    """读取必需的环境变量；缺失时显式失败并指出变量名。

    刻意**不提供默认值**：历史默认值是公开可知的弱口令（形如 `postgres` + 数字后缀），
    一旦兜底，compose 忘记注入变量时应用会静默连上同一个弱口令的库 ——
    既看不出配置缺失，也把弱口令固化进部署。
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"缺少必需的环境变量 {name}。请参照仓库根目录的 .env.example 配置后重试，"
            f"不要把真实口令写入受版本控制的文件。"
        )
    return value


# 从环境变量获取数据库配置
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
# 口令必填：本行在**导入期**执行，因此缺失会让应用在启动阶段就终止，
# 而不是等到第一次建连才暴露（与 core.security 的 JWT 校验同一风格）。
POSTGRES_PASSWORD = _require_env("POSTGRES_PASSWORD")
POSTGRES_DB = os.getenv("POSTGRES_DB", "industry_assistant")

DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"


def resolve_text2sql_url() -> str:
    """返回 text2sql 使用的连接串（T10）。

    优先 `TEXT2SQL_DATABASE_URL` —— 指向只有 SELECT 权限的角色后，即使 T09 的
    解析式 SQL 校验被绕过，写操作也会在**数据库层**被拒绝（唯一根治手段）；
    未配置则回退主库 `DATABASE_URL`，此时解析式校验是唯一防线。
    """
    return os.getenv("TEXT2SQL_DATABASE_URL") or DATABASE_URL


# 连接池参数取保守值（依据见 tickets.md T12）：
# - pool_size=5 / max_overflow=10：单实例应用的常规并发足够；Postgres 默认 max_connections=100，
#   多个进程（后端 + 数据初始化脚本）同时连接也不会逼近上限。
# - pool_pre_ping=True：取连接前先探活，避免连接被数据库/中间件静默断开后报 "connection already closed"。
# - pool_recycle=1800：小于常见的数据库/代理空闲超时（如 1h），让连接在被动断开前主动轮换。
DB_POOL_SIZE = 5
DB_MAX_OVERFLOW = 10
DB_POOL_RECYCLE = 1800  # 秒

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=DB_POOL_RECYCLE,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """获取数据库会话的依赖函数"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
