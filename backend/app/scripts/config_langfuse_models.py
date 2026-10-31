# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
LangFuse 模型价格配置脚本

批量配置项目用到的 LLM 模型价格，配置后 LangFuse 能自动计算每次调用和每个研究请求的成本。

使用方式:
    cd backend
    python -m app.scripts.config_langfuse_models

    或直接运行:
    python app/scripts/config_langfuse_models.py

前置条件:
    1. LangFuse 服务已启动（docker compose -f docker/langfuse/docker-compose.yml up -d）
    2. backend/.env 中配置了 LANGFUSE_BASE_URL、LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY

注意:
    模型价格基于阿里云百炼 2025 年公开定价（单位：元/千 token）。
    请在 https://help.aliyun.com/zh/model-studio/getting-started/models 核对最新价格后修改。
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

LANGFUSE_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "http://localhost:3000")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

# ====================================================================
# 模型价格表（单位：元/千 token）
# 数据来源：阿里云百炼 2025 年公开定价
# 请核对最新价格: https://help.aliyun.com/zh/model-studio/getting-started/models
# ====================================================================
MODEL_PRICES = [
    {
        "model": "deepseek-v3.2",
        "input_price": 0.002,
        "output_price": 0.008,
        "description": "DeepSeek V3.2 — 通用大模型，性价比高"
    },
    {
        "model": "qwen-plus",
        "input_price": 0.004,
        "output_price": 0.012,
        "description": "通义千问 Plus — 快速模型，适合搜索阶段"
    },
    {
        "model": "qwen-max",
        "input_price": 0.02,
        "output_price": 0.06,
        "description": "通义千问 Max — 最强模型，适合复杂推理"
    },
    {
        "model": "qwen-turbo",
        "input_price": 0.002,
        "output_price": 0.006,
        "description": "通义千问 Turbo — 轻量快速模型"
    },
    {
        "model": "deepseek-r1",
        "input_price": 0.004,
        "output_price": 0.016,
        "description": "DeepSeek R1 — 推理增强模型"
    },
]


def configure_model_prices():
    """配置模型价格到 LangFuse"""
    if not LANGFUSE_PUBLIC_KEY or not LANGFUSE_SECRET_KEY:
        print("[ERROR] 未配置 LangFuse API key，请在 .env 中设置 LANGFUSE_PUBLIC_KEY 和 LANGFUSE_SECRET_KEY")
        sys.exit(1)

    auth = (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)
    api_base = f"{LANGFUSE_BASE_URL}/api/public"

    print(f"[INFO] LangFuse 地址: {LANGFUSE_BASE_URL}")
    print(f"[INFO] 开始配置 {len(MODEL_PRICES)} 个模型的价格...")
    print()

    success_count = 0
    fail_count = 0

    for model_info in MODEL_PRICES:
        model_name = model_info["model"]
        input_price = model_info["input_price"]
        output_price = model_info["output_price"]

        # LangFuse model definition API
        # 价格单位：per token（LangFuse 内部用 per token，我们传入 per 1000 tokens 的价格除以 1000）
        # 实际上 LangFuse 的价格字段单位是 per token，所以需要转换
        payload = {
            "model": model_name,
            "inputPrice": input_price / 1000,   # 转换为 per token
            "outputPrice": output_price / 1000,  # 转换为 per token
            "unit": "TOKENS",
            "currency": "CNY",
        }

        try:
            # 先尝试删除已有的定义（如果存在），再创建
            resp = requests.post(
                f"{api_base}/model-definitions",
                auth=auth,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )

            if resp.status_code in (200, 201):
                print(f"  [OK] {model_name}: 输入 {input_price} 元/千token, 输出 {output_price} 元/千token")
                success_count += 1
            elif resp.status_code == 409:
                # 已存在，尝试 PUT 更新
                resp2 = requests.put(
                    f"{api_base}/model-definitions",
                    auth=auth,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                if resp2.status_code in (200, 201):
                    print(f"  [OK] {model_name}: 已更新价格")
                    success_count += 1
                else:
                    print(f"  [WARN] {model_name}: 更新失败 {resp2.status_code} - {resp2.text[:200]}")
                    fail_count += 1
            else:
                print(f"  [FAIL] {model_name}: {resp.status_code} - {resp.text[:200]}")
                fail_count += 1

        except requests.exceptions.ConnectionError:
            print(f"  [ERROR] 无法连接 LangFuse 服务，请确认服务已启动: {LANGFUSE_BASE_URL}")
            sys.exit(1)
        except Exception as e:
            print(f"  [ERROR] {model_name}: {e}")
            fail_count += 1

    print()
    print(f"[完成] 成功: {success_count}, 失败: {fail_count}")
    if success_count > 0:
        print()
        print("现在可以在 LangFuse UI 的 Tracing → Costs 页面查看成本分析")
        print(f"访问: {LANGFUSE_BASE_URL}")


if __name__ == "__main__":
    configure_model_prices()
