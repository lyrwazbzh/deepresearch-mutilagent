# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""聊天附件路由"""
import logging
import os
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks, Form
from sqlalchemy.orm import Session

from core.database import get_db
from core.upload_security import (
    ensure_supported_extension,
    remove_quietly,
    sanitize_extension,
    save_upload,
)
from models.chat import ChatAttachment, ChatSession
from models.user import User
from router.auth_router import get_current_user_required
from schemas.chat import AttachmentResponse, AttachmentListResponse

logger = logging.getLogger(__name__)

# 全文件无匿名端点：上传 / 详情 / 列表 / 删除都作用在会话数据上，一律要求认证。
# 与 `document_router.py`（T04）保持同一写法：依赖挂在 router 级，新增端点自动受保护。
# 背景：本文件原先是全仓**唯一**仍用 `get_current_user`（可选认证）的路由 —— 未登录即可
# 读取任意会话的附件清单、按 ID 取附件详情、乃至删除任意附件及其落盘文件（终审 §4 发现）。
#
# 归属校验（终审 §4 复检 N1 / T55）：`get_current_user_required` 只解决「匿名」这一半；
# 读/删三个端点还必须确认资源属于**当前用户**，否则任何已登录用户凭一个 UUID 即可跨用户
# 读删。校验以**会话归属**为准（`ChatSession.user_id`，NOT NULL）而非 `ChatAttachment.user_id`
# （后者 nullable，历史行可能为空），与 `session_router.py` 的 7 处会话查询同型。
# 「非本人」一律收敛为 404（而非 403），避免泄漏「该 UUID 存在」这一事实。
router = APIRouter(
    prefix="/attachments",
    tags=["聊天附件"],
    dependencies=[Depends(get_current_user_required)],
)

# 文件上传目录
UPLOAD_DIR = "/tmp/chat_attachments"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 支持的文件类型
ALLOWED_EXTENSIONS = {
    # 文档类型
    '.pdf', '.docx', '.doc', '.txt', '.md', '.html', '.xlsx', '.xls', '.pptx', '.ppt',
    # 图片类型
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp',
    # 代码类型
    '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.xml', '.csv',
}


# 扩展名工具已收拢到 core.upload_security（sanitize_extension），避免三处实现各自漂移；
# 白名单 ALLOWED_EXTENSIONS 仍由本路由自行维护。


def attachment_to_response(att: ChatAttachment) -> AttachmentResponse:
    """将附件模型转换为响应"""
    return AttachmentResponse(
        id=str(att.id),
        session_id=str(att.session_id),
        message_id=str(att.message_id) if att.message_id else None,
        filename=att.filename,
        file_type=att.file_type,
        file_size=att.file_size,
        status=att.status,
        error_message=att.error_message,
        created_at=att.created_at,
    )


async def process_attachment(attachment_id: str, file_path: str, db_session_factory):
    """后台处理附件（提取文本内容）"""
    import logging
    logger = logging.getLogger("AttachmentProcessor")

    db = db_session_factory()
    try:
        att = db.query(ChatAttachment).filter(ChatAttachment.id == attachment_id).first()
        if not att:
            return

        att.status = "processing"
        db.commit()

        try:
            content_text = ""
            ext = sanitize_extension(att.filename)

            # 简单的文本提取（可以扩展为使用 DocMind）
            if ext in {'.txt', '.md', '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.xml', '.csv', '.html'}:
                # 直接读取文本文件
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content_text = f.read()
            elif ext == '.pdf':
                # PDF 需要特殊处理，这里暂时跳过
                # 可以后续集成 DocMind 或 PyPDF2
                content_text = f"[PDF 文件: {att.filename}]"
            elif ext in {'.docx', '.doc'}:
                # Word 文档需要特殊处理
                content_text = f"[Word 文档: {att.filename}]"
            elif ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
                # 图片文件
                content_text = f"[图片: {att.filename}]"
            else:
                content_text = f"[文件: {att.filename}]"

            # 限制内容长度
            if len(content_text) > 50000:
                content_text = content_text[:50000] + "\n...[内容已截断]"

            att.content_text = content_text
            att.status = "completed"
            att.error_message = None

        except Exception as e:
            logger.error(f"处理附件失败: {e}")
            att.status = "failed"
            att.error_message = str(e)

        db.commit()

    except Exception as e:
        logger.error(f"附件处理异常: {e}")
    finally:
        db.close()


@router.post("", response_model=AttachmentResponse, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session_id: str = Form(...),
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """上传聊天附件"""
    # 解析 session_id
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的会话ID格式"
        )

    # 验证会话存在且属于当前用户（非本人 → 404，不区分「不存在」与「非本人」）
    session = (
        db.query(ChatSession)
        .filter(
            ChatSession.id == session_uuid,
            ChatSession.user_id == current_user.id,
        )
        .first()
    )
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在"
        )

    # 验证文件类型：先剥离客户端塞入的目录片段，再比对白名单
    ext = ensure_supported_extension(file.filename, ALLOWED_EXTENSIONS)

    # 落盘名由服务端生成、限长读取、失败清理统一 —— 详见 core.upload_security.save_upload（T44）。
    # **不使用 file.filename**：后者完全由客户端控制，"../../" 片段足以把文件写出 UPLOAD_DIR。
    file_path = await save_upload(file, UPLOAD_DIR, extension=ext)

    # 获取文件大小
    file_size = os.path.getsize(file_path)

    # 创建附件记录
    att = ChatAttachment(
        session_id=session_uuid,
        user_id=current_user.id,
        filename=file.filename,
        file_type=ext[1:] if ext else "unknown",
        file_size=file_size,
        file_path=file_path,
        status="pending",
    )
    db.add(att)
    db.commit()
    db.refresh(att)

    # 后台处理附件
    from core.database import SessionLocal
    background_tasks.add_task(
        process_attachment,
        str(att.id),
        file_path,
        SessionLocal
    )

    return attachment_to_response(att)


@router.get("/{attachment_id}", response_model=AttachmentResponse)
async def get_attachment(
    attachment_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取附件详情（仅限本人会话的附件）"""
    try:
        att_uuid = UUID(attachment_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的附件ID格式"
        )

    # 归属过滤 + 存在性过滤合并为一次查询：非本人 → 查不到 → 404
    att = (
        db.query(ChatAttachment)
        .join(ChatSession, ChatAttachment.session_id == ChatSession.id)
        .filter(
            ChatAttachment.id == att_uuid,
            ChatSession.user_id == current_user.id,
        )
        .first()
    )
    if not att:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="附件不存在"
        )

    return attachment_to_response(att)


@router.get("/session/{session_id}", response_model=AttachmentListResponse)
async def get_session_attachments(
    session_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取会话的所有附件（仅限本人会话）"""
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的会话ID格式"
        )

    # 验证会话存在且属于当前用户（非本人 → 404，不区分「不存在」与「非本人」）
    session = (
        db.query(ChatSession)
        .filter(
            ChatSession.id == session_uuid,
            ChatSession.user_id == current_user.id,
        )
        .first()
    )
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在"
        )

    attachments = db.query(ChatAttachment).filter(
        ChatAttachment.session_id == session_uuid
    ).order_by(ChatAttachment.created_at.desc()).all()

    return AttachmentListResponse(
        attachments=[attachment_to_response(att) for att in attachments],
        total=len(attachments),
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """删除附件（仅限本人会话的附件）"""
    try:
        att_uuid = UUID(attachment_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的附件ID格式"
        )

    # 归属过滤 + 存在性过滤合并：非本人 → 404，且**不会删到任何东西**
    att = (
        db.query(ChatAttachment)
        .join(ChatSession, ChatAttachment.session_id == ChatSession.id)
        .filter(
            ChatAttachment.id == att_uuid,
            ChatSession.user_id == current_user.id,
        )
        .first()
    )
    if not att:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="附件不存在"
        )

    # 删除落盘文件。清理失败只告警、**绝不上抛** —— 原先这里是裸 `os.remove`
    # 加 `except Exception: pass`：删不掉既不留任何痕迹，`db.delete(att)` 又照跑，
    # 于是记录没了、文件永久成孤儿；且与 document_router / knowledge_router 的
    # 既定标准相反。本处是同一缺陷家族**漏掉的第三处**（终审 §4 中-1；
    # 同族：T53 / P-17、T56 / 终审 §4 N3）。
    if att.file_path:
        remove_quietly(att.file_path, logger=logger)

    db.delete(att)
    db.commit()
    return None
