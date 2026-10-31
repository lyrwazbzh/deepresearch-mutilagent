# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
知识库检索服务 - 基于 Milvus

功能：
1. retrieve_content - 从指定集合检索内容
2. retrieve_from_knowledge_base - 从知识库检索内容
"""

from typing import List, Dict, Any, Optional
from service.milvus_service import get_milvus_service
from service.embedding_service import generate_embedding
import logging

logger = logging.getLogger(__name__)


def retrieve_content(
    indexNames: str,
    question: str,
    top_k: int = 5,
    kb_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    检索相关内容

    Args:
        indexNames: 集合名称（知识库索引）
        question: 查询问题
        top_k: 返回结果数量
        kb_id: 知识库ID（可选过滤）

    Returns:
        检索结果列表
    """
    try:
        # 1. 生成查询向量
        query_vectors = generate_embedding([question])
        if not query_vectors or len(query_vectors) == 0:
            logger.warning("生成查询向量失败")
            return []

        query_vector = query_vectors[0]

        # 2. 执行向量搜索
        milvus = get_milvus_service()
        results = milvus.search(
            collection_name=indexNames,
            query_vector=query_vector,
            top_k=top_k,
            kb_id=kb_id,
        )

        # 3. 格式化结果
        extracted_data = []
        for i, result in enumerate(results, start=1):
            message = {
                "id": i,
                "document_id": result.get("doc_id", "N/A"),
                "document_name": result.get("filename", "N/A"),
                "content_with_weight": result.get("content", ""),
                "score": result.get("score", 0),
            }
            extracted_data.append(message)

        return extracted_data

    except Exception as e:
        logger.warning(f"检索错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


def retrieve_from_knowledge_base(
    kb_name: str,
    question: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    从知识库检索内容

    Args:
        kb_name: 知识库名称
        question: 查询问题
        top_k: 返回结果数量

    Returns:
        检索结果列表
    """
    # 将知识库名称转换为集合名称
    collection_name = f"kb_{kb_name}".lower().replace(" ", "_")
    return retrieve_content(collection_name, question, top_k)


def format_local_search_results(
    results: List[Dict[str, Any]],
    kb_name: str,
) -> List[Dict[str, Any]]:
    """把 ``retrieve_from_knowledge_base`` 的原始结果转成**唯一的本地检索 shape**。

    背景（T45 方案 B）：本地检索结果的形状此前有**三处**独立实现且字段已分叉 ——
    ``deep_research_v2/agents/scout.py``（V2 主链路）用 ``title`` / ``site_name`` /
    ``is_local``，而 ``dr_g.py`` 与 ``tool_executor.py``（V1 备选路线）用 ``name`` /
    ``siteName`` / ``source``，连 ``url`` 前缀都不同（``local://kb/<kb>/<doc>`` vs
    ``local://<kb>/<doc>``）。字段名不一致的代价是：**取错名字不会报错，只会静默拿到
    空标题 /「未知来源」**。现统一以 scout.py 的字段为准，三处共用本函数。

    ``summary`` 截断到 500 字符、``snippet`` 到 200 —— 沿用 scout.py 的既有口径，
    避免把整个 chunk（可能数千字）灌进 SSE 与去重比较。
    """
    formatted: List[Dict[str, Any]] = []
    for r in results:
        content = r.get("content_with_weight", "") or ""
        formatted.append({
            "url": f"local://kb/{kb_name}/{r.get('document_id', 'unknown')}",
            "title": r.get("document_name", "N/A"),
            "summary": content[:500],
            "snippet": content[:200],
            "site_name": "本地知识库",
            "date": "",
            "score": r.get("score", 0),
            "is_local": True,
            "kb_name": kb_name,
            "doc_id": r.get("document_id"),
        })
    return formatted
