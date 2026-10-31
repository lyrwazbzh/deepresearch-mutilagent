"""DeepResearchGraph 的构造缝（T65）—— 注入协作者这条路径的契约。

这个文件是该缝的**测试面**，只钉两件事：
① 六个角色确实由调用方给定；
② 这条路径**不读配置**（否则「测试不必依赖 .env」这个收益就是假的）。
"""

import service.deep_research_v2.agents as agents_package

# 兜底：与同目录其它用例一致 —— 精简环境下 `agents` 包缺真实类时补占位名，
# 保证 graph 模块可导入（本机真实类存在，正常环境下不产生副作用）。
for _agent_name in (
    "ChiefArchitect",
    "DeepScout",
    "CodeWizard",
    "CriticMaster",
    "LeadWriter",
    "DataAnalyst",
):
    if not hasattr(agents_package, _agent_name):
        setattr(agents_package, _agent_name, type(_agent_name, (), {}))

import service.deep_research_v2.graph as graph_module
from service.deep_research_v2.graph import DeepResearchGraph

_ROLES = ("architect", "scout", "data_analyst", "wizard", "critic", "writer")


def test_injecting_agents_does_not_read_config(monkeypatch):
    """注入路径不读配置：把 get_config 换成会抛错的替身，构造仍须成功。"""

    def _boom():
        raise AssertionError("注入 agents 时不应读取配置")

    monkeypatch.setattr(graph_module, "get_config", _boom)

    graph = DeepResearchGraph(
        agents={role: object() for role in _ROLES},
        checkpoint_service=None,
    )

    assert graph.checkpoint_service is None


def test_injected_agents_are_wired_to_the_six_roles():
    """六个角色必须原样落到实例属性上（不多不少）。"""
    sentinels = {role: object() for role in _ROLES}

    graph = DeepResearchGraph(agents=sentinels, checkpoint_service=None)

    assert {role: getattr(graph, role) for role in _ROLES} == sentinels
