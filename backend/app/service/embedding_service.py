# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
Embedding 服务 - OpenAI 兼容 /embeddings（供应商由环境变量切换）

功能：
1. generate_embedding - 生成向量。缺省仍是 DashScope text-embedding-v4；可经
   EMBEDDING_MODEL / EMBEDDING_BASE_URL / EMBEDDING_API_KEY 切换供应商
   （如硅基流动 BAAI/bge-m3，细节见函数 docstring）。
2. rerank_similarity - 使用 DashScope Rerank 重排序（仍读 DASHSCOPE_API_KEY）
"""

import os
from typing import List, Optional, Tuple
import numpy as np
from openai import OpenAI
from llama_index.core.data_structs import Node
from llama_index.core.schema import NodeWithScore
from llama_index.postprocessor.dashscope_rerank import DashScopeRerank

from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)
load_dotenv()


def generate_embedding(
    text: str | List[str],
    api_key: str = None,
    base_url: str = None,
    model_name: str | None = None,
    dimensions: int | None = None,
    encoding_format: str = "float",
    max_batch_size: int = 10
) -> Optional[List[float] | List[List[float]]]:
    """
    生成文本的向量嵌入（OpenAI 兼容 /embeddings，供应商由环境变量决定）

    环境变量（默认值保持 DashScope 旧行为，切换供应商零代码改动）：
    - EMBEDDING_API_KEY    优先；缺省回退 DASHSCOPE_API_KEY
    - EMBEDDING_BASE_URL   优先；缺省回退 DASHSCOPE_BASE_URL，再缺省 DashScope 兼容模式
    - EMBEDDING_MODEL      缺省 text-embedding-v4（如硅基流动 BAAI/bge-m3）
    - EMBEDDING_DIMENSIONS 可选。**不设置就不向供应商传 dimensions** —— 硅基流动 bge-m3
      对该参数直接 400（code=20015，实测 2026-09-14），而 DashScope v4 需要它；
      bge-m3 固定 1024 维，与 milvus_service.vector_dim 一致，故无需设置。

    Args:
        text: 单个文本或文本列表
        api_key: API密钥（默认从环境变量获取）
        base_url: API基础URL（默认从环境变量获取）
        model_name: 模型名称（缺省读 EMBEDDING_MODEL）
        dimensions: 向量维度；None 时不向供应商传该参数
        encoding_format: 编码格式
        max_batch_size: 最大批量大小（DashScope 限制为10）

    Returns:
        单个文本时返回向量，文本列表时返回向量列表
    """
    api_key = api_key or os.getenv("EMBEDDING_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    base_url = base_url or os.getenv("EMBEDDING_BASE_URL") or os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model_name = model_name or os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    if dimensions is None:
        dimensions = os.getenv("EMBEDDING_DIMENSIONS")

    if not api_key:
        logger.warning("错误: 缺少 EMBEDDING_API_KEY / DASHSCOPE_API_KEY 环境变量")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        logger.warning(f"初始化 OpenAI 客户端失败: {e}")
        return None

    # 单个文本
    if isinstance(text, str):
        try:
            kwargs = {"encoding_format": encoding_format}
            if dimensions:
                kwargs["dimensions"] = int(dimensions)
            completion = client.embeddings.create(
                model=model_name,
                input=text,
                **kwargs
            )
            return completion.data[0].embedding
        except Exception as e:
            logger.warning(f"Embedding 请求失败: {e}")
            return None

    # 文本列表 - 分批处理
    if isinstance(text, list):
        all_embeddings = []

        for i in range(0, len(text), max_batch_size):
            batch = text[i:i + max_batch_size]

            try:
                kwargs = {"encoding_format": encoding_format}
                if dimensions:
                    kwargs["dimensions"] = int(dimensions)
                completion = client.embeddings.create(
                    model=model_name,
                    input=batch,
                    **kwargs
                )
                batch_embeddings = [item.embedding for item in completion.data]
                all_embeddings.extend(batch_embeddings)
            except Exception as e:
                logger.warning(f"Embedding 批量请求失败 (batch {i // max_batch_size + 1}): {e}")
                all_embeddings.extend([None] * len(batch))

        return all_embeddings

    return None


def rerank_similarity(
    query: str,
    texts: List[str],
    top_n: int = None
) -> Tuple[np.ndarray, None]:
    """
    使用 DashScope Rerank 对文本进行重排序

    Args:
        query: 查询文本
        texts: 待排序的文本列表
        top_n: 返回前N个结果（默认返回全部）

    Returns:
        (scores, None) - 分数数组和占位符
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")

    if not api_key:
        logger.warning("错误: 缺少 DASHSCOPE_API_KEY 环境变量")
        return np.array([]), None

    top_n = top_n or len(texts)

    # 创建节点列表
    nodes = [NodeWithScore(node=Node(text=text), score=1.0) for text in texts]

    # 初始化 DashScopeRerank
    dashscope_rerank = DashScopeRerank(top_n=top_n, api_key=api_key)

    # 执行重排序
    results = dashscope_rerank.postprocess_nodes(nodes, query_str=query)

    # 提取分数
    scores = np.array([res.score for res in results])

    return scores, None
