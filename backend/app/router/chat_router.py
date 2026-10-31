# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

from typing import Dict, Any, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.status import HTTP_200_OK, HTTP_500_INTERNAL_SERVER_ERROR
from sqlalchemy.orm import Session

from core.database import get_db
from models.chat import ChatAttachment, ChatSession
from models.user import User
from router.auth_router import get_current_user_required
from service import DocumentService, WebSearchService, ChatService, SessionService, ServiceConfig
from service.retrieval_service import retrieve_content
from schemas import ChatRequest, LegacySessionResponse, ChatWithAttachmentsRequest

# Create router instance
# 全文件无匿名端点：会话创建（/session）与三个版本的补全接口（/completion/v1、/completion、/completion/v3）
# 都属用户级对话操作，一律要求认证。在 router 级挂一次依赖，避免逐端点重复（新增端点也自动受保护）。
router = APIRouter(
    prefix="/chat",
    tags=["chat"],
    dependencies=[Depends(get_current_user_required)],
)

# Get service instances
def get_services():
    config = ServiceConfig.get_api_config()
    doc_service = DocumentService(
        base_url=config['base_url'],
        api_key=config['api_key']
    )
    web_service = WebSearchService(api_key=config.get('serper_api_key'))
    session_service = SessionService()
    chat_service = ChatService(doc_service, web_service, session_service)
    return {
        "chat_service": chat_service,
        "session_service": session_service,
        "default_dataset_id": config['default_dataset_id']
    }



@router.post("/session", response_model=LegacySessionResponse, status_code=HTTP_200_OK)
async def create_session(
    services: Dict[str, Any] = Depends(get_services)
):
    """
    创建新的聊天会话

    Returns:
        新创建的会话信息
    """
    session_service = services["session_service"]

    try:
        session_data = session_service.create_session()
        return LegacySessionResponse(**session_data)
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建会话失败: {str(e)}"
        )

@router.post("/completion/v1", status_code=HTTP_200_OK)
async def chat_completion(
    request: ChatRequest,
    services: Dict[str, Any] = Depends(get_services)
):
    """
    聊天补全接口，结合知识库检索和Web搜索，进行问答

    Args:
        request: 包含用户问题和会话信息的请求体

    Returns:
        流式响应，包含检索内容和模型生成内容
    """
    chat_service = services["chat_service"]
    default_dataset_id = services["default_dataset_id"]
    session_service = services["session_service"]

    # 验证会话ID（如果提供）
    if request.session_id:
        session = session_service.get_session(request.session_id)
        if not session:
            # 如果会话不存在，创建新会话
            session_data = session_service.create_session()
            request.session_id = session_data["session_id"]

    # 创建异步生成器函数
    async def generate_response():
        try:
            # 从知识库检索文档
            knowledge_docs = []
            if request.search_knowledge:
                knowledge_docs = chat_service.retrieve_from_knowledge_base(
                    question=request.question,
                    dataset_id=default_dataset_id
                )

            # 从Web搜索检索信息
            web_docs = []
            if request.search_web:
                web_docs = chat_service.retrieve_from_web(
                    question=request.question
                )

            # 合并文档并重排
            all_docs = knowledge_docs + web_docs
            reranked_docs = chat_service.rerank_documents(
                question=request.question,
                documents=all_docs
            )

            # 生成流式回答
            for message_chunk in chat_service.get_chat_completion(
                session_id=request.session_id,
                question=request.question,
                retrieved_content=reranked_docs
            ):
                yield message_chunk

        except Exception as e:
            # 错误处理
            error_message = f"event: error\ndata: {str(e)}\n\n"
            yield error_message

    # 返回流式响应
    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream"
    )

@router.post("/completion", status_code=HTTP_200_OK)
async def chat_completion_v2(
    request: ChatRequest,
    services: Dict[str, Any] = Depends(get_services)
):
    """
    聊天补全接口v2版本，使用policy_documents索引进行检索，采用Dealer检索引擎，结合Web搜索进行问答

    Args:
        request: 包含用户问题和会话信息的请求体

    Returns:
        流式响应，包含检索内容和模型生成内容
    """
    chat_service = services["chat_service"]
    default_dataset_id = services["default_dataset_id"]
    session_service = services["session_service"]

    # 验证会话ID（如果提供）
    if request.session_id:
        session = session_service.get_session(request.session_id)
        if not session:
            # 如果会话不存在，创建新会话
            session_data = session_service.create_session()
            request.session_id = session_data["session_id"]

    # 创建异步生成器函数
    async def generate_response():
        try:
            # 从政策文档索引检索（使用新的检索方式）
            policy_docs = []
            if request.search_knowledge:
                # 使用 retrieve_content 函数进行检索，默认索引为 policy_documents
                retrieved_data = retrieve_content(
                    indexNames="policy_documents",
                    question=request.question
                )

                # 转换数据格式以适配现有系统
                policy_docs = []
                for item in retrieved_data:
                    policy_docs.append({
                        "id": item["id"],
                        "content": item["content_with_weight"],
                        "source": f"{item['document_name']} (ID: {item['document_id']})",
                        "document_id": item["document_id"],
                        "document_name": item["document_name"]
                    })

            # 从Web搜索检索信息
            web_docs = []
            if request.search_web:
                web_docs = chat_service.retrieve_from_web(
                    question=request.question
                )

            # 合并文档并重排
            all_docs = policy_docs + web_docs
            reranked_docs = chat_service.rerank_documents(
                question=request.question,
                documents=all_docs
            )

            # 生成流式回答
            for message_chunk in chat_service.get_chat_completion(
                session_id=request.session_id,
                question=request.question,
                retrieved_content=reranked_docs
            ):
                yield message_chunk

        except Exception as e:
            # 错误处理
            error_message = f"event: error\ndata: {str(e)}\n\n"
            yield error_message

    # 返回流式响应
    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream"
    )


@router.post("/completion/v3", status_code=HTTP_200_OK)
async def chat_completion_with_attachments(
    request: ChatWithAttachmentsRequest,
    # 鉴权必须是**第一个**被解析的依赖（T64 的顺序层锁）：FastAPI 按签名顺序逐个解析并短路，
    # 排在 `get_services` 之后会让未登录请求也真的构造服务、把配置状态泄漏给匿名调用方。
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
    services: Dict[str, Any] = Depends(get_services)
):
    """
    聊天补全接口v3版本，支持附件的问答

    附件内容会被提取并加入到上下文中，用于回答问题。

    Args:
        request: 包含用户问题、附件ID列表和会话信息的请求体

    Returns:
        流式响应，包含检索内容和模型生成内容
    """
    chat_service = services["chat_service"]
    session_service = services["session_service"]

    # 验证会话ID（如果提供）
    if request.session_id:
        session = session_service.get_session(request.session_id)
        if not session:
            session_data = session_service.create_session()
            request.session_id = session_data["session_id"]

    # 获取附件内容
    attachment_contents = []
    if request.attachment_ids:
        for att_id in request.attachment_ids:
            try:
                att_uuid = UUID(att_id)
            except ValueError:
                continue
            # 归属校验（§4 补审 / T72）：原实现只按 `id` 过滤，**无** join、**无** owner 判断
            # —— 任意已登录用户凭一个 UUID 即可读出他人附件正文，而该正文会被注入模型上下文。
            # 与 `attachment_router`（T55）同型，且同样是「T55 的源码锁只罩单个文件、结构性
            # 照不到本文件」漏下的。以**会话归属**为准（`ChatSession.user_id`，NOT NULL）而非
            # `ChatAttachment.user_id`（nullable，历史行可能为空）；非本人查不到即视为不存在，
            # 与无效 UUID 走同一条静默跳过路径，不泄漏「该 UUID 存在」这一事实。
            att = (
                db.query(ChatAttachment)
                .join(ChatSession, ChatAttachment.session_id == ChatSession.id)
                .filter(
                    ChatAttachment.id == att_uuid,
                    ChatSession.user_id == current_user.id,
                )
                .first()
            )
            if att and att.content_text and att.status == "completed":
                attachment_contents.append({
                    "filename": att.filename,
                    "content": att.content_text[:10000],  # 限制每个附件内容长度
                })

    async def generate_response():
        try:
            # 从政策文档索引检索
            policy_docs = []
            if request.search_knowledge:
                retrieved_data = retrieve_content(
                    indexNames="policy_documents",
                    question=request.question
                )
                for item in retrieved_data:
                    policy_docs.append({
                        "id": item["id"],
                        "content": item["content_with_weight"],
                        "source": f"{item['document_name']} (ID: {item['document_id']})",
                        "document_id": item["document_id"],
                        "document_name": item["document_name"]
                    })

            # 从Web搜索检索信息
            web_docs = []
            if request.search_web:
                web_docs = chat_service.retrieve_from_web(
                    question=request.question
                )

            # 合并文档并重排
            all_docs = policy_docs + web_docs
            reranked_docs = chat_service.rerank_documents(
                question=request.question,
                documents=all_docs
            )

            # 构建附件上下文
            attachment_context = ""
            if attachment_contents:
                attachment_context = "\n\n=== 用户上传的附件内容 ===\n"
                for att in attachment_contents:
                    attachment_context += f"\n--- {att['filename']} ---\n{att['content']}\n"
                attachment_context += "\n=== 附件内容结束 ===\n\n"

            # 修改问题，加入附件上下文
            enhanced_question = request.question
            if attachment_context:
                enhanced_question = f"用户问题：{request.question}\n\n请结合以下上传的附件内容来回答问题：{attachment_context}"

            # 生成流式回答
            for message_chunk in chat_service.get_chat_completion(
                session_id=request.session_id,
                question=enhanced_question,
                retrieved_content=reranked_docs
            ):
                yield message_chunk

        except Exception as e:
            error_message = f"event: error\ndata: {str(e)}\n\n"
            yield error_message

    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream"
    )
