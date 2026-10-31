# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""SSE 事件序列化工具（从 service/dr_g.py 原样抽离，见 tickets.md T15）。

抽离目的：research_router 只需要这一个序列化函数，此前却因此反向依赖
V1 备选路线模块 dr_g，模糊了模块边界。本模块为中立公共位置，无任何重依赖。
"""

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


#: SSE 结束哨兵 —— 全仓只在这里定义，且只在最外层发射一次（T67）。
SSE_DONE = "data: [DONE]\n\n"


def sse_frame(event_data: Union[Dict[str, Any], str]) -> str:
    """把事件编成一条 SSE 帧 —— **深度研究链路**上唯一的「信封」实现（T67）。

    T67 之前，这根链路上帧的拼装有三种写法：`deep_research_v2/service.py` 的私有
    `_format_sse`、router 里 6 处 `serialize_event(...)` 再手写 `data: …` 前后缀、
    以及 router 给 V1 已序列化字符串套壳。现在这条链路上产出帧的地方都走本函数。

    **范围**（T67 批次审查 F2 的更正）：本函数只收拢**深度研究**链路。chat 链路的
    `chat_service.py` 自有一套**具名事件**帧（`event: end` + `data: [DONE]`），形状与
    本函数不同、且属另一条路由，刻意不在此收拢 —— 见 TRACKER 未闭合项 **P-23**。
    故本函数**不是**「全仓唯一」的信封实现。

    入参两态：
    - `dict`：结构化事件 —— 在这里序列化**一次**（V2 链路即此形态，见 T67）；
    - `str`：已经序列化好的 JSON 字符串 —— 仅 V1 备选路线用。`dr_g.research_stream`
      是 PRD NG-3 的有意保留实现，其「产出 JSON 字符串」的既有契约不在本票范围内，
      故信封对这一态做透传，而不是二次编码（否则会把字符串再 JSON 转义一遍）。
    """
    payload = event_data if isinstance(event_data, str) else serialize_event(event_data)
    return f"data: {payload}\n\n"
