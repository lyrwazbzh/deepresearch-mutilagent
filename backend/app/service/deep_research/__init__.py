# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch 多智能体研究主链。

六个专家 Agent（Architect / Scout / DataAnalyst / Wizard / Critic / Writer）共享
ResearchState，由 AsyncIO 状态机调度：Plan → Research → Analyze → Write → Review，
覆盖不足补搜，其余问题定向修订，默认最多 3 轮。

使用方式：
```python
from service.deep_research.service import DeepResearchService

service = DeepResearchService()
async for event in service.research("中国AI芯片市场分析"):
    print(event)
```
"""

from .state import (
    ResearchState,
    ResearchPhase,
    Section,
    Fact,
    DataPoint,
    Chart,
    CriticFeedback,
    create_initial_state
)

from .graph import DeepResearchGraph, create_research_graph

from .agents import (
    ChiefArchitect,
    DeepScout,
    CodeWizard,
    CriticMaster,
    LeadWriter
)

__all__ = [
    # State
    'ResearchState',
    'ResearchPhase',
    'Section',
    'Fact',
    'DataPoint',
    'Chart',
    'CriticFeedback',
    'create_initial_state',

    # Graph
    'DeepResearchGraph',
    'create_research_graph',

    # Agents
    'ChiefArchitect',
    'DeepScout',
    'CodeWizard',
    'CriticMaster',
    'LeadWriter',
]
