# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

import logging
import os
import time
import uuid
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query
from starlette.status import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

from service import DocumentService, ServiceConfig
from service.docmind_service import process_document_with_docmind
from router.auth_router import get_current_user_required
from core.upload_security import (
    ensure_supported_extension,
    remove_quietly,
    save_upload,
)
from schemas.document import (
    DeleteDocumentsRequest,
    RetrieveDocumentsRequest,
    UploadDocumentResponse,
    DocumentListResponse,
    DeleteDocumentsResponse
)

# Create router instance
# 全文件无匿名端点：上传 / 列出 / 删除 / 检索都属于文档数据操作，一律要求认证。
# 因此在 router 级挂一次依赖，避免逐端点重复（新增端点也自动受保护）。
router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(get_current_user_required)],
)

logger = logging.getLogger(__name__)

# Get service configuration
def get_document_service():
    config = ServiceConfig.get_api_config()
    return DocumentService(
        base_url=config['base_url'],
        api_key=config['api_key']
    ), config['default_dataset_id']

# 支持的文件类型
SUPPORTED_FILE_TYPES = {
    '.pdf', '.docx', '.xlsx', '.xls', '.txt'
}

# 上传临时目录；落盘文件名由服务端生成，不使用客户端文件名（见 core.upload_security）
UPLOAD_TEMP_DIR = os.getenv("DOCUMENT_UPLOAD_TEMP_DIR", "/tmp")

@router.post("/upload", status_code=HTTP_200_OK, response_model=UploadDocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    index_name: str = Query(default="policy_documents", description="Index name for storing the document"),
):
    """
    Upload a document and process it using local parsing and ES storage.
    Supports multiple file formats: PDF, DOCX, XLSX, TXT etc.
    
    Args:
        file: Document file to upload
        index_name: Index name for storing the document (default: policy_documents)
    
    Returns:
        JSON response with upload status and document details
    """
    try:
        # 验证文件类型：按「剥掉目录片段后的扩展名」比对白名单
        file_extension = ensure_supported_extension(file.filename, SUPPORTED_FILE_TYPES)

        # 生成唯一的文档ID
        document_id = str(uuid.uuid4())

        # 保存上传的文件到临时位置。
        # 落盘名由服务端生成（uuid + 规范化扩展名）、限长读取、失败清理，全部收敛到
        # core.upload_security.save_upload —— **不使用 file.filename**（该字符串完全由
        # 客户端控制，"../../" 片段足以把文件写到 /tmp 之外，事实 F-02），三入口共用（T44）。
        temp_file_path = await save_upload(file, UPLOAD_TEMP_DIR, extension=file_extension)
        
        # 使用 DocMind 处理文档并存储到 Milvus
        processing_result = process_document_with_docmind(
            file_path=temp_file_path,
            file_name=file.filename,
            index_name=index_name,
            chunk_size=500
        )

        # 清理临时文件。经 `remove_quietly` 收敛：文件删不掉（Windows 句柄占用 →
        # PermissionError）不能让「文档已处理成功」的请求反而失败（T56 / 终审 §4 N3）。
        if os.path.exists(temp_file_path):
            remove_quietly(temp_file_path, logger=logger)

        # 检查处理结果
        if not processing_result["success"]:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Document processing failed: {processing_result['message']}"
            )

        return {
            "status": "success",
            "message": processing_result["message"],
            "document_count": processing_result["document_count"]
        }
        
    except HTTPException:
        # 重新抛出HTTP异常
        raise
    except Exception as e:
        # 清理临时文件。这里是**最关键的一处**：此前是裸 `os.remove`，它一旦抛出（同一
        # 个文件很可能正是刚才失败的那一个），异常会从这个 except 里逸出，把下面精心
        # 准备的 500 覆盖成裸 OSError（T56 / 终审 §4 N3）。
        if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
            remove_quietly(temp_file_path, logger=logger)
        
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing document: {str(e)}"
        )

@router.get("/list", status_code=HTTP_200_OK, response_model=DocumentListResponse)
async def get_document_list(
    page: int = Query(1, description="Page number"),
    page_size: int = Query(10, description="Number of items per page"),
    keywords: Optional[str] = Query(None, description="Keywords to filter document titles"),
    document_id: Optional[str] = Query(None, description="Filter by specific document ID"),
    service_info: tuple = Depends(get_document_service)
):
    """
    Get a list of documents with filtering options.
    
    Args:
        page: Page number for pagination
        page_size: Number of items per page
        keywords: Optional keywords to filter document titles
        document_id: Optional document ID to filter specific document
        
    Returns:
        JSON response with list of documents and their details
    """
    doc_service, dataset_id = service_info
    
    try:
        # Get documents from service
        response = doc_service.get_documents(
            dataset_id=dataset_id,
            page=page,
            page_size=page_size,
            keywords=keywords,
            document_id=document_id
        )
        
        # 检查响应中的错误码
        if response.get("code") != 0:
            # 如果是因为没有文档导致的特定错误，返回空列表而不是错误
            if 'document None' in response.get("message", ""):
                return {
                    "code": 0,
                    "data": {
                        "docs": [],
                        "total": 0
                    }
                }
            # 其他错误正常抛出异常
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Failed to retrieve documents: {response}"
            )
        
        # Return the response directly to match the expected format
        return response
        
    except Exception as e:
        # 如果是因为没有文档导致的异常，返回空列表
        error_msg = str(e)
        if "document None" in error_msg or "You don't own the document None" in error_msg:
            return {
                "code": 0,
                "data": {
                    "docs": [],
                    "total": 0
                }
            }
        
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving documents: {str(e)}"
        )

@router.post("/delete", status_code=HTTP_200_OK, response_model=DeleteDocumentsResponse)
async def delete_documents(
    request: DeleteDocumentsRequest,
    service_info: tuple = Depends(get_document_service)
):
    """
    Delete documents from a dataset.
    
    Args:
        request: Body containing document_ids list
        
    Returns:
        JSON response with deletion status
    """
    doc_service, dataset_id = service_info
    
    try:
        # Call service to delete documents
        response = doc_service.delete_documents(
            dataset_id=dataset_id,
            document_ids=request.document_ids
        )
        
        # Check for errors in response
        if response.get("code") != 0:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Failed to delete documents: {response}"
            )
        
        # Return successful response
        return {
            "code": 0,
            "message": "Documents deleted successfully",
            "data": response.get("data", {})
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting documents: {str(e)}"
        )

@router.post("/retrieve", status_code=HTTP_200_OK)
async def retrieve_documents(
    request: RetrieveDocumentsRequest,
    service_info: tuple = Depends(get_document_service)
):
    """
    Retrieve documents based on a question.
    
    Args:
        request: Body containing the question and optional document_ids
        
    Returns:
        JSON response with retrieved documents
    """
    doc_service, dataset_id = service_info
    
    try:
        # Call service to retrieve documents
        response = doc_service.retrieve_documents(
            question=request.question,
            dataset_ids=[dataset_id],
            document_ids=request.document_ids
        )
        
        # Check for errors in response
        if response.get("code") != 0:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=f"Failed to retrieve documents: {response}"
            )
        
        # Return the response directly
        return response
        
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving documents: {str(e)}"
        ) 