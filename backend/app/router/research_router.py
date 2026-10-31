# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

from typing import Dict, Any, Optional, Literal, List
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
import logging

from service import ResearchService, ServiceConfig
from core.serialization import sse_frame  # 中立公共位置（T15）：不再反向依赖 V1 备选路线模块 dr_g
from core.research_cancel import request_cancel  # 取消协议的中立位（T68）：本模块不再自己拥有它
from models.user import User
from router.auth_router import get_current_user_required
from service.checkpoint_service import (
    CheckpointNotFound,
    OutlineApprovalConflict,
    get_checkpoint_service,
)

# V2 导入
from service.deep_research_v2.service import DeepResearchV2Service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ResearchRouter")

# 创建路由实例
router = APIRouter(prefix="/research", tags=["research"])

# 请求模型
class ResearchRequest(BaseModel):
    """深度研究请求模型"""
    query: str
    session_id: Optional[str] = None  # 会话 ID（用于检查点保存）
    max_iterations: Optional[int] = 3
    kb_name: Optional[str] = None  # 本地知识库名称
    search_web: Optional[bool] = None  # 是否搜索网络 (兼容旧版)
    search_local: Optional[bool] = None  # 是否搜索本地知识库 (兼容旧版)
    search_modes: Optional[list] = None  # 搜索模式: ['web', 'local'] (新版)
    search_database: bool = False
    database_profile_id: Optional[str] = None
    # 版本选择：v2 = 多智能体架构（deep_research_v2/，默认，推荐）；
    # v1 = ReAct 备选路线（service/dr_g.py + react_controller.py + tool_executor.py，
    # 有意保留，NG-3 —— 不得当死代码删除）。
    version: Optional[Literal["v1", "v2"]] = "v2"

    class Config:
        json_schema_extra = {
            "example": {
                "query": "中国安责险的市场现状和未来发展趋势是什么？请提供具体数据支持。",
                "session_id": None,
                "max_iterations": 3,
                "kb_name": None,
                "search_modes": ["web", "local"],
                "version": "v2"
            }
        }

    def get_search_web(self) -> bool:
        """获取是否搜索网络"""
        if self.search_modes is not None:
            return 'web' in self.search_modes
        return self.search_web if self.search_web is not None else True

    def get_search_local(self) -> bool:
        """获取是否搜索本地知识库"""
        if self.search_modes is not None:
            return 'local' in self.search_modes
        return self.search_local if self.search_local is not None else False

    def get_search_database(self) -> bool:
        """获取是否允许访问预配置的只读数据库。"""
        if self.search_modes is not None:
            return "database" in self.search_modes
        return self.search_database


class OutlineSectionRequest(BaseModel):
    id: str
    title: str
    description: str
    section_type: Literal["qualitative", "quantitative", "mixed"] = "mixed"
    requires_data: bool = False
    requires_chart: bool = False

    @field_validator("id", "title", "description")
    @classmethod
    def require_nonblank_section_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class ResearchQuestionRequest(BaseModel):
    id: str
    text: str

    @field_validator("id", "text")
    @classmethod
    def require_nonblank_question_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class ApproveOutlineRequest(BaseModel):
    outline_revision: str
    sections: List[OutlineSectionRequest] = Field(min_length=3, max_length=12)
    research_questions: List[ResearchQuestionRequest] = Field(
        min_length=3,
        max_length=12,
    )

    @field_validator("outline_revision")
    @classmethod
    def require_nonblank_revision(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("outline_revision must not be blank")
        return value

class LazyResearchService:
    """Resolve the legacy V1 service only when its stream is actually consumed."""

    def __init__(self):
        self._service = None

    def _resolve(self):
        if self._service is None:
            config = ServiceConfig.get_api_config()
            self._service = ResearchService(
                search_api_key=config.get("bochaai_api_key"),
                llm_api_key=config.get("dashscope_api_key"),
                llm_base_url=config.get("dashscope_base_url"),
            )
        return self._service

    async def research_stream(self, **kwargs):
        async for event in self._resolve().research_stream(**kwargs):
            yield event


def get_research_service():
    """Return a lazy legacy service so V2 requests never read V1 credentials."""
    return {"research_service": LazyResearchService()}


def get_research_service_v2(max_iterations: int | None = None):
    """获取 V2 研究服务实例（使用配置文件中的模型设置）"""
    # 直接创建服务，配置从 llm_config.py 读取
    return DeepResearchV2Service(max_iterations=max_iterations)

@router.post("/stream", status_code=HTTP_200_OK)
async def stream_research(
    request: ResearchRequest,
    current_user: User = Depends(get_current_user_required),
    services: Dict[str, Any] = Depends(get_research_service),
):
    """
    深度研究接口 - 流式输出

    对用户的研究问题执行全面的深度研究，包括问题分解、网络搜索、信息整合、数据分析和报告生成。
    使用 Server-Sent Events (SSE) 格式流式返回整个研究过程和结果。

    支持两个版本：
    - v1: 传统 ReAct 架构
    - v2: 多智能体协作网络（推荐）

    Args:
        request: 包含研究问题和配置的请求体

    Returns:
        流式响应，包含研究过程和结果的 SSE 格式数据
    """
    # 根据版本选择服务
    if request.version == "v2":
        search_web = request.get_search_web()
        search_local = request.get_search_local()
        search_database = request.get_search_database()
        if search_database and not request.database_profile_id:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="database_profile_id is required when database search is enabled",
            )
        logger.info(f"Using DeepResearch V2 for query: {request.query[:50]}... (session_id: {request.session_id}, search_web={search_web}, search_local={search_local})")
        service_v2 = get_research_service_v2(request.max_iterations)

        async def generate_sse_v2():
            try:
                async for event in service_v2.research(
                    query=request.query,
                    session_id=request.session_id,
                    kb_name=request.kb_name,
                    search_web=search_web,
                    search_local=search_local,
                    search_database=search_database,
                    database_profile_id=request.database_profile_id,
                    user_id=str(current_user.id),
                ):
                    yield event
            except Exception as e:
                logger.error(f"V2 Research error: {e}")
                yield sse_frame({"type": "error", "content": str(e)})

        return StreamingResponse(
            generate_sse_v2(),
            media_type="text/event-stream"
        )

    # V1 协作者直到流真正执行时才解析配置；V2 不读取 V1 凭据。
    research_service = services["research_service"]

    async def generate_sse():
        try:
            async for event in research_service.research_stream(
                query=request.query,
                max_iterations=request.max_iterations,
                kb_name=request.kb_name,
                search_web=request.search_web,
                search_local=request.search_local
            ):
                # V1 路线在 dr_g 内已序列化，这里只套信封（T67）
                yield sse_frame(event)
        except Exception as e:
            # 统一走中立公共信封，确保错误事件的 JSON 格式一致（T67）
            yield sse_frame({"type": "error", "content": str(e)})

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream"
    )

@router.get("/stream", status_code=HTTP_200_OK)
async def stream_research_get(
    query: str = Query(..., description="研究问题", example="中国安责险的市场现状和未来发展趋势是什么？"),
    max_iterations: int = Query(3, description="最大迭代次数", ge=1, le=5),
    kb_name: Optional[str] = Query(None, description="本地知识库名称"),
    search_web: bool = Query(True, description="是否搜索网络"),
    search_local: bool = Query(True, description="是否搜索本地知识库"),
    search_database: bool = Query(False, description="是否检索预配置只读数据库"),
    database_profile_id: Optional[str] = Query(None, description="只读数据库配置标识"),
    version: Literal["v1", "v2"] = Query("v2", description="版本: v1 或 v2"),
    current_user: User = Depends(get_current_user_required),
    services: Dict[str, Any] = Depends(get_research_service),
):
    """
    深度研究接口 - GET方式流式输出

    对用户的研究问题执行全面的深度研究，包括问题分解、网络搜索、信息整合、数据分析和报告生成。
    使用 Server-Sent Events (SSE) 格式流式返回整个研究过程和结果。

    支持两个版本：
    - v1: 传统 ReAct 架构
    - v2: 多智能体协作网络（推荐）

    Args:
        query: 研究问题
        max_iterations: 最大迭代次数（范围：1-5）
        version: 版本选择 (v1 或 v2)

    Returns:
        流式响应，包含研究过程和结果的 SSE 格式数据
    """
    # 根据版本选择服务
    if version == "v2":
        if search_database and not database_profile_id:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="database_profile_id is required when database search is enabled",
            )
        logger.info(f"Using DeepResearch V2 (GET) for query: {query[:50]}...")
        service_v2 = get_research_service_v2(max_iterations)

        async def generate_sse_v2():
            try:
                async for event in service_v2.research(
                    query=query,
                    kb_name=kb_name,
                    user_id=str(current_user.id),
                    search_web=search_web,
                    search_local=search_local,
                    search_database=search_database,
                    database_profile_id=database_profile_id,
                ):
                    yield event
            except Exception as e:
                logger.error(f"V2 Research error: {e}")
                yield sse_frame({"type": "error", "content": str(e)})

        return StreamingResponse(
            generate_sse_v2(),
            media_type="text/event-stream"
        )

    # V1 原有逻辑
    research_service = services["research_service"]

    async def generate_sse():
        try:
            async for event in research_service.research_stream(
                query=query,
                max_iterations=max_iterations,
                kb_name=kb_name,
                search_web=search_web,
                search_local=search_local
            ):
                # V1 路线在 dr_g 内已序列化，这里只套信封（T67）
                yield sse_frame(event)
        except Exception as e:
            # 统一走中立公共信封，确保错误事件的 JSON 格式一致（T67）
            yield sse_frame({"type": "error", "content": str(e)})

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream"
    )


@router.get("/test-wizard", status_code=HTTP_200_OK)
async def test_wizard_endpoint(
    _current_user: User = Depends(get_current_user_required),
):
    """
    测试 CodeWizard 数据分析功能（绕过搜索阶段）

    使用模拟数据直接测试图表生成功能。
    """
    from service.deep_research_v2.agents.wizard import CodeWizard
    from service.deep_research_v2.state import ResearchState, ResearchPhase, create_initial_state
    from config.llm_config import get_config

    # 使用配置创建 CodeWizard 实例
    llm_config = get_config()
    wizard = CodeWizard(
        llm_api_key=llm_config.api_key,
        llm_base_url=llm_config.base_url,
        model=llm_config.agents.wizard.model
    )

    # 使用 create_initial_state 创建完整的状态
    mock_state = create_initial_state("中国GDP增长率分析", "test-session")

    # 设置分析阶段
    mock_state["phase"] = ResearchPhase.ANALYZING.value

    # 添加测试大纲
    mock_state["outline"] = [
        {"id": "sec_1", "title": "GDP增长趋势", "requires_chart": True, "section_type": "quantitative", "description": "分析中国近年GDP增长趋势"}
    ]

    # 添加测试数据点
    mock_state["data_points"] = [
        {"name": "2020年GDP增长率", "value": 2.3, "unit": "%"},
        {"name": "2021年GDP增长率", "value": 8.1, "unit": "%"},
        {"name": "2022年GDP增长率", "value": 3.0, "unit": "%"},
        {"name": "2023年GDP增长率", "value": 5.2, "unit": "%"},
        {"name": "2024年GDP增长率", "value": 5.0, "unit": "%"}
    ]

    # 添加测试事实
    mock_state["facts"] = [
        {
            "id": "fact_1",
            "content": "2020年中国GDP增长率为2.3%，是新冠疫情影响下的低谷",
            "source_url": "http://example.com",
            "source_name": "国家统计局",
            "related_sections": ["sec_1"]
        },
        {
            "id": "fact_2",
            "content": "2021年中国GDP强劲反弹，增长率达到8.1%",
            "source_url": "http://example.com",
            "source_name": "国家统计局",
            "related_sections": ["sec_1"]
        }
    ]

    try:
        # 执行数据分析
        result_state = await wizard.process(mock_state)

        charts = result_state.get("charts", [])
        return {
            "success": True,
            "charts_count": len(charts),
            "charts": [
                {
                    "title": c.get("title", ""),
                    "type": c.get("type", ""),
                    "has_image": bool(c.get("image_base64")),
                    "image_length": len(c.get("image_base64", "")) if c.get("image_base64") else 0
                }
                for c in charts
            ],
            "errors": result_state.get("errors", [])
        }
    except Exception as e:
        logger.error(f"Test wizard error: {e}")
        import traceback
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }


@router.post("/cancel/{session_id}", status_code=HTTP_200_OK)
async def cancel_research(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
):
    """
    取消正在进行的研究任务

    Args:
        session_id: 会话ID

    Returns:
        取消确认信息
    """
    try:
        checkpoint_service = get_checkpoint_service()
        if not checkpoint_service.get_checkpoint_info(
            session_id,
            user_id=str(current_user.id),
        ):
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail="Checkpoint not found",
            )

        # 写取消标志（Redis，300s TTL）—— 协议本身已迁到中立模块（T68）
        request_cancel(session_id)
        logger.info(f"Research cancelled for session: {session_id}")
        return {"success": True, "message": "Research cancellation requested"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel research: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ============ 检查点 API ============

@router.get("/checkpoint/{session_id}", status_code=HTTP_200_OK)
async def get_checkpoint(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
):
    """
    获取研究检查点信息

    Args:
        session_id: 会话ID

    Returns:
        检查点信息（不含完整状态）
    """
    try:
        checkpoint_service = get_checkpoint_service()
        info = checkpoint_service.get_checkpoint_info(
            session_id,
            user_id=str(current_user.id),
        )
        if info:
            return {"success": True, "checkpoint": info}
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail="Checkpoint not found",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get checkpoint: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/checkpoint/{session_id}/full", status_code=HTTP_200_OK)
async def get_full_checkpoint(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
):
    """
    获取完整的研究检查点（包含 UI 状态和报告）

    用于恢复未完成的研究状态到前端

    Args:
        session_id: 会话ID

    Returns:
        完整检查点数据，包含:
        - state_json: 后端研究状态
        - ui_state_json: 前端 UI 状态（研究步骤、图表等）
        - final_report: 最终报告内容
    """
    try:
        checkpoint_service = get_checkpoint_service()
        full_data = checkpoint_service.load_full_checkpoint(
            session_id,
            user_id=str(current_user.id),
        )
        if full_data:
            return {"success": True, "checkpoint": full_data}
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail="Checkpoint not found",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get full checkpoint: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/checkpoints", status_code=HTTP_200_OK)
async def list_checkpoints(
    current_user: User = Depends(get_current_user_required),
    status: Optional[str] = Query(None, description="过滤状态: running/paused/completed/failed"),
    limit: int = Query(20, ge=1, le=100, description="返回数量限制")
):
    """
    列出研究检查点

    Args:
        status: 状态过滤
        limit: 返回数量限制

    Returns:
        检查点列表
    """
    try:
        checkpoint_service = get_checkpoint_service()
        checkpoints = checkpoint_service.list_checkpoints(
            user_id=str(current_user.id),
            status=status,
            limit=limit,
        )
        return {"success": True, "checkpoints": checkpoints, "total": len(checkpoints)}
    except Exception as e:
        logger.error(f"Failed to list checkpoints: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/checkpoint/{session_id}", status_code=HTTP_200_OK)
async def delete_checkpoint(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
):
    """
    删除研究检查点

    Args:
        session_id: 会话ID

    Returns:
        删除结果
    """
    try:
        checkpoint_service = get_checkpoint_service()
        success = checkpoint_service.delete_checkpoint(
            session_id,
            user_id=str(current_user.id),
        )
        if success:
            return {"success": True, "message": "Checkpoint deleted"}
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail="Checkpoint not found",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete checkpoint: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/outline/{session_id}/approve", status_code=HTTP_200_OK)
async def approve_research_outline(
    session_id: str,
    request: ApproveOutlineRequest,
    current_user: User = Depends(get_current_user_required),
):
    """Atomically approve an outline and continue the research as SSE."""
    try:
        checkpoint_service = get_checkpoint_service()
        state = checkpoint_service.approve_outline(
            session_id=session_id,
            user_id=str(current_user.id),
            outline_revision=request.outline_revision,
            sections=[section.model_dump() for section in request.sections],
            research_questions=[
                question.model_dump() for question in request.research_questions
            ],
        )
    except CheckpointNotFound as exc:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except OutlineApprovalConflict as exc:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    service_v2 = get_research_service_v2()

    async def generate_sse():
        try:
            async for event in service_v2.research(
                query=state.get("query", ""),
                session_id=session_id,
                resume=True,
                user_id=str(current_user.id),
            ):
                yield event
        except Exception as exc:
            logger.error(f"Approved research continuation error: {exc}")
            yield sse_frame({"type": "error", "content": str(exc)})

    return StreamingResponse(generate_sse(), media_type="text/event-stream")


@router.post("/resume/{session_id}", status_code=HTTP_200_OK)
async def resume_research(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
):
    """
    恢复研究任务（从检查点）

    Args:
        session_id: 会话ID

    Returns:
        流式响应，从检查点继续研究
    """
    try:
        checkpoint_service = get_checkpoint_service()
        info = checkpoint_service.get_checkpoint_info(
            session_id,
            user_id=str(current_user.id),
        )

        if not info:
            raise HTTPException(
                status_code=HTTP_404_NOT_FOUND,
                detail="Checkpoint not found",
            )

        if info.get("status") == "completed":
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail="Research already completed"
            )

        if info.get("phase") == "awaiting_outline_approval":
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail="Outline approval required before research can resume",
            )

        # 使用 V2 服务恢复
        service_v2 = get_research_service_v2()

        async def generate_sse():
            try:
                async for event in service_v2.research(
                    query=info.get("query", ""),
                    session_id=session_id,
                    resume=True,
                    user_id=str(current_user.id),
                ):
                    yield event
            except Exception as e:
                logger.error(f"Resume research error: {e}")
                yield sse_frame({"type": "error", "content": str(e)})

        return StreamingResponse(
            generate_sse(),
            media_type="text/event-stream"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to resume research: {e}")
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
