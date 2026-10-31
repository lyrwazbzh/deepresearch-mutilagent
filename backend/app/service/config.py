# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

import os
from typing import Dict, Any

class ServiceConfig:
    """Configuration for service API connections"""
    
    @staticmethod
    def get_api_config() -> Dict[str, Any]:
        """
        Get API configuration from environment variables or default settings

        Returns:
            Dictionary with API configuration
        """
        return {
            'base_url': os.environ.get('API_BASE_URL', 'http://localhost:9380'),
            # 以下三项历史上带有真实凭据作为默认值，已随源码泄露到公开仓库。
            # 现改为空默认值，不再静默使用泄露凭据。缺失时三者都是**响亮失败**（T47 已补齐）：
            #   - api_key / default_dataset_id：调用方 DocumentService 显式校验后抛错；
            #   - serper_api_key：WebSearchService.search() 在发请求前抛 ValueError（方案 C，
            #     调用期显式报错），不再带空 X-API-KEY 静默发请求。
            'api_key': os.environ.get('API_KEY', ''),
            'default_dataset_id': os.environ.get('DEFAULT_DATASET_ID', ''),
            'serper_api_key': os.environ.get('SERPER_API_KEY', ''),
            'milvus_host': os.environ.get('MILVUS_HOST', 'localhost'),
            'milvus_port': int(os.environ.get('MILVUS_PORT', '19530')),
            'policy_collection': os.environ.get('POLICY_COLLECTION', 'policy_documents'),
            # DeepResearch API keys
            'bochaai_api_key': os.environ.get('BOCHA_API_KEY', ''),
            'dashscope_api_key': os.environ.get('DASHSCOPE_API_KEY', ''),
            'dashscope_base_url': os.environ.get('DASHSCOPE_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
        } 