# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""SSE 事件序列化工具：深度研究链路的中立公共信封。"""

import json
import logging
from typing import Any, Dict, Union


def serialize_event(event_data: Dict[str, Any]) -> str:
    """将事件数据序列化为JSON字符串"""
    def json_serializer(obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, Exception):
            return str(obj)
        raise TypeError(f"Type {type(obj)} not serializable")

    try:
        return json.dumps(event_data, default=json_serializer, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to serialize event: {e}")
        return json.dumps({"type": "error", "content": f"Serialization error: {e}"})


#: SSE 结束哨兵 —— 全仓只在这里定义，且只在最外层发射一次。
SSE_DONE = "data: [DONE]\n\n"


def sse_frame(event_data: Union[Dict[str, Any], str]) -> str:
    """把事件编成一条 SSE 帧 —— 深度研究链路上的信封实现。

    chat 链路自有具名事件帧，形状不同，不在此收拢。

    入参：
    - `dict`：结构化事件，在此序列化一次；
    - `str`：已序列化 JSON，透传套壳（避免二次转义）。
    """
    payload = event_data if isinstance(event_data, str) else serialize_event(event_data)
    return f"data: {payload}\n\n"
