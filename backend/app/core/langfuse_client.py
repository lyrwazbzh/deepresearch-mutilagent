# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
LangFuse 监控客户端（基于 LangFuse Python SDK v4 / OpenTelemetry）

提供 LLM 全链路追踪能力，支持:
- 创建 trace 串联完整 Agent 链路
- 创建 span 标记每个研究阶段
- 创建 generation 记录 LLM 调用细节（prompt/response/token/成本）
- 优雅降级：LangFuse 未配置或不可用时不影响业务

使用方式:
    from app.core.langfuse_client import is_enabled, start_trace, start_span, start_generation

    with start_trace(name="deep_research", session_id=sid, user_id=uid) as trace:
        with start_span(name="plan_phase"):
            with start_generation(name="architect_llm", model="deepseek-v3.2") as gen:
                response = client.chat.completions.create(...)
                gen.update(output=response, usage_details={...})

配置（在 backend/.env 中，LangFuse SDK v4 自动读取）:
    LANGFUSE_ENABLED=true
    LANGFUSE_BASE_URL=http://localhost:3000
    LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
    LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx

要求:
    - langfuse Python SDK >= 4.0.0（OTel-based 版本）
    - LangFuse 平台 >= 4.0.0
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# 从环境变量读取启用开关（SDK v4 自动读取 LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY）
LANGFUSE_ENABLED = os.getenv("LANGFUSE_ENABLED", "false").lower() == "true"

# 全局客户端单例
_client = None
_init_attempted = False


def _get_client():
    """获取 LangFuse 客户端单例（懒加载）"""
    global _client, _init_attempted

    if _init_attempted:
        return _client

    _init_attempted = True

    if not LANGFUSE_ENABLED:
        logger.info("[LangFuse] 监控未启用（LANGFUSE_ENABLED=false）")
        return None

    try:
        from langfuse import get_client
        # SDK v4 的 get_client() 从环境变量读取配置，返回全局单例
        _client = get_client()
        if _client is None:
            logger.warning("[LangFuse] get_client() 返回 None，请检查 LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL 配置")
            return None
        logger.info(f"[LangFuse] 监控客户端已初始化（SDK v4 / OpenTelemetry）")
    except ImportError:
        logger.warning("[LangFuse] langfuse 包未安装，请运行: pip install 'langfuse>=4.0.0'")
    except Exception as e:
        logger.warning(f"[LangFuse] 客户端初始化失败（监控将降级为禁用）: {e}")

    return _client


def is_enabled() -> bool:
    """检查 LangFuse 监控是否可用"""
    return _get_client() is not None


def get_client():
    """获取 LangFuse 客户端（外部可用来做高级操作）"""
    return _get_client()


@contextmanager
def start_trace(
    name: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    input: Optional[Dict[str, Any]] = None,
    output: Optional[Dict[str, Any]] = None,
):
    """
    创建一个 trace（顶层链路），对应一次完整的研究请求。

    SDK v4 中，trace 由第一个 root observation 隐式定义，trace 属性（session_id/user_id）
    通过 propagate_attributes 设置。

    用法:
        with start_trace(name="deep_research", session_id=sid, user_id=uid) as trace:
            # 业务逻辑
            trace.update(output={"status": "success"})

    未启用时返回 no-op 上下文，业务代码无需判断。
    """
    client = _get_client()
    if client is None:
        yield _NoOpObservation()
        return

    try:
        from langfuse import propagate_attributes

        # SDK v4: 用 start_as_current_observation 创建 root span，它隐式定义了 trace
        with client.start_as_current_observation(as_type="span", name=name) as span:
            # 用 propagate_attributes 设置 trace 级别属性（user_id, session_id, metadata）
            # 这些属性会被传播到所有子 observation
            with propagate_attributes(
                user_id=user_id,
                session_id=session_id,
                metadata=metadata,
            ):
                if input:
                    span.update(input=input)

                try:
                    yield span
                    if output:
                        span.update(output=output)
                except Exception as e:
                    try:
                        span.update(level="ERROR", status_message=str(e))
                    except Exception:
                        pass
                    raise
    except Exception as e:
        logger.debug(f"[LangFuse] trace 创建失败（降级为 no-op）: {e}")
        yield _NoOpObservation()


@contextmanager
def start_span(
    name: str,
    input: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    在当前 trace 下创建一个 span（对应一个 phase 或子步骤）。
    SDK v4 基于 OpenTelemetry，span 自动嵌套到当前上下文。

    用法:
        with start_span(name="plan_phase", input={"query": q}):
            # 这里的 LLM 调用（用 start_generation）会自动挂到此 span 下
            result = agent.call_llm(...)

    未启用时返回 no-op 上下文。
    """
    client = _get_client()
    if client is None:
        yield _NoOpObservation()
        return

    try:
        kwargs = {"as_type": "span", "name": name}
        if input:
            kwargs["input"] = input
        if metadata:
            kwargs["metadata"] = metadata

        with client.start_as_current_observation(**kwargs) as span:
            yield span
    except Exception as e:
        logger.debug(f"[LangFuse] span 创建失败（降级为 no-op）: {e}")
        yield _NoOpObservation()


@contextmanager
def start_generation(
    name: str,
    model: Optional[str] = None,
    input: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    创建一个 LLM generation observation，用于记录 LLM 调用细节。

    SDK v4 中，generation 是专门的 observation 类型，支持 model/usage_details/cost_details。

    用法:
        with start_generation(name="architect_llm", model="deepseek-v3.2") as gen:
            response = client.chat.completions.create(...)
            gen.update(
                output=response.choices[0].message.content,
                usage_details={
                    "input": response.usage.prompt_tokens,
                    "output": response.usage.completion_tokens,
                }
            )

    未启用时返回 no-op 上下文。
    """
    client = _get_client()
    if client is None:
        yield _NoOpObservation()
        return

    try:
        kwargs = {"as_type": "generation", "name": name}
        if model:
            kwargs["model"] = model
        if input:
            kwargs["input"] = input
        if metadata:
            kwargs["metadata"] = metadata

        with client.start_as_current_observation(**kwargs) as gen:
            yield gen
    except Exception as e:
        logger.debug(f"[LangFuse] generation 创建失败（降级为 no-op）: {e}")
        yield _NoOpObservation()


def flush():
    """强制 flush 所有缓冲的 trace（用于进程退出前确保数据上报）"""
    client = _get_client()
    if client:
        try:
            client.flush()
        except Exception as e:
            logger.debug(f"[LangFuse] flush 失败: {e}")


def shutdown():
    """关闭客户端（进程退出时调用）"""
    client = _get_client()
    if client:
        try:
            client.flush()
        except Exception:
            pass


class _NoOpObservation:
    """未启用 LangFuse 时的空 observation 实现"""
    def update(self, **kwargs): pass
    def end(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


# 进程退出时自动 flush
import atexit
atexit.register(shutdown)
