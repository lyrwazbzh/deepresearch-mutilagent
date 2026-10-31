# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
T08 回归测试：Scout 本地知识库检索的集合名修复。

背景（事实 F-08）：`scout.py` 曾硬编码 `collection_name="knowledge_base"`，
而用户文档入库走 `kb_{知识库名}`（`knowledge_router.py`），
导致 DeepResearch 的本地知识库搜索搜不到任何用户文档。

修复口径（ticket T08 方案 A）：
- Scout 不自己拼集合名，改用 `retrieval_service.retrieve_from_knowledge_base`
  （该函数内部完成 kb_name -> kb_{kb_name} 的转换，与入库侧一致）；
- `kb_name` 为空时跳过本地检索（与 V1 `search_local and kb_name` 的门控语义一致）。

所有用例均 mock 掉 retrieval_service，不依赖真实 Milvus。
"""

import asyncio
from pathlib import Path

from service import retrieval_service
from service.deep_research_v2.agents import scout as scout_module
from service.deep_research_v2.agents.scout import DeepScout

SCOUT_SOURCE_PATH = (
    Path(__file__).resolve().parents[3]
    / "app" / "service" / "deep_research_v2" / "agents" / "scout.py"
)


def _make_scout() -> DeepScout:
    """构造一个不触网的 DeepScout（OpenAI client 仅保存配置，不发请求）。"""
    return DeepScout(
        llm_api_key="test-only-key",
        llm_base_url="http://localhost:9999/v1",
        search_api_key="test-only-key",
    )


def test_scout_no_longer_hardcodes_generic_collection_name():
    """验收 #1：scout.py 中不再存在硬编码的公共集合名字面量 "knowledge_base"。

    注意：检索入口函数名 retrieve_from_knowledge_base 包含 knowledge_base 子串，
    与 ticket 验收命令 `! grep -n '"knowledge_base"'` 一致，只匹配带引号的字面量。
    """
    source = SCOUT_SOURCE_PATH.read_text(encoding="utf-8")
    assert '"knowledge_base"' not in source, (
        "scout.py 仍硬编码了公共集合名 knowledge_base，"
        "用户文档实际入库到 kb_{知识库名} 集合，二者不匹配"
    )


def test_local_search_routes_through_retrieval_service(monkeypatch):
    """本地检索必须走 retrieve_from_knowledge_base（集合名单一来源），并透传 kb_name。"""
    calls = {}

    def fake_retrieve(kb_name, question, top_k=5):
        calls["kb_name"] = kb_name
        calls["question"] = question
        calls["top_k"] = top_k
        return [
            {
                "id": 1,
                "document_id": "doc-1",
                "document_name": "安责险管理办法.pdf",
                "content_with_weight": "第八条 保险公司应当按照规定提取事故预防技术服务费用。",
                "score": 0.87,
            }
        ]

    monkeypatch.setattr(scout_module, "retrieve_from_knowledge_base", fake_retrieve)
    monkeypatch.setattr(scout_module, "MILVUS_AVAILABLE", True)

    scout = _make_scout()
    results = asyncio.run(
        scout._execute_local_search("事故预防技术服务费用", kb_name="安责险法规")
    )

    assert calls == {
        "kb_name": "安责险法规",
        "question": "事故预防技术服务费用",
        "top_k": 10,
    }
    assert len(results) == 1
    item = results[0]
    assert item["url"] == "local://kb/安责险法规/doc-1"
    assert item["title"] == "安责险管理办法.pdf"
    assert item["summary"].startswith("第八条")
    assert item["snippet"] == item["summary"][:200]
    assert item["score"] == 0.87
    assert item["is_local"] is True
    assert item["doc_id"] == "doc-1"


def test_local_search_skips_without_kb_name(monkeypatch):
    """kb_name 为空时跳过检索（无法确定集合），不得伪造结果。"""

    def fail_retrieve(*args, **kwargs):
        raise AssertionError("kb_name 为空时不应发起任何检索调用")

    monkeypatch.setattr(scout_module, "retrieve_from_knowledge_base", fail_retrieve)

    scout = _make_scout()
    results = asyncio.run(scout._execute_local_search("任意查询", kb_name=None))
    assert results == []


def test_retrieve_from_knowledge_base_derives_kb_prefixed_collection(monkeypatch):
    """集合名转换规则：kb_{kb_name}.lower().replace(" ", "_")，与入库侧一致。"""
    captured = {}

    def fake_retrieve_content(collection_name, question, top_k=5, kb_id=None):
        captured["collection_name"] = collection_name
        return []

    monkeypatch.setattr(retrieval_service, "retrieve_content", fake_retrieve_content)

    retrieval_service.retrieve_from_knowledge_base("Demo KB", "query text")
    assert captured["collection_name"] == "kb_demo_kb"

    retrieval_service.retrieve_from_knowledge_base("安责险法规", "query text")
    assert captured["collection_name"] == "kb_安责险法规"


def test_graph_run_wires_kb_name_into_initial_state(monkeypatch):
    """kb_name 必须能从 research() 一路进入初始状态（此前在 _research_stream 被丢弃）。"""
    import service.deep_research_v2.agents as agents_package
    from service.deep_research_v2.state import (
        create_initial_state as real_create_initial_state,
    )

    # create_initial_state 层：kb_name 落入状态
    state = real_create_initial_state(
        "topic", "session-1", search_local=True, kb_name="demo"
    )
    assert state["kb_name"] == "demo"

    # conftest 把 service.deep_research_v2(.agents) 注册成了命名空间桩，
    # 真实的 agents/__init__.py 不会执行，因此先给桩补上占位 agent 类再导入 graph
    # （与 tests/service/test_graph_outline_checkpoint.py 同一模式）。
    for agent_name in (
        "ChiefArchitect",
        "DeepScout",
        "CodeWizard",
        "CriticMaster",
        "LeadWriter",
        "DataAnalyst",
    ):
        if not hasattr(agents_package, agent_name):
            setattr(agents_package, agent_name, type(agent_name, (), {}))

    import service.deep_research_v2.graph as graph_module
    from service.deep_research_v2.graph import DeepResearchGraph

    # graph.run 层：签名接受 kb_name 并传入 create_initial_state。
    # 用 spy 捕获参数，并在拿到首个事件（research_start）后立即关闭生成器，
    # 不触发后续任何 agent / LLM 调用。
    captured = {}

    def spy_create_initial_state(query, session_id, **kwargs):
        captured.update(kwargs)
        return real_create_initial_state(query, session_id, **kwargs)

    monkeypatch.setattr(graph_module, "create_initial_state", spy_create_initial_state)

    # 走真实构造器：run() 首个 yield 前不触碰任何 agent，故六个角色给占位对象即可
    graph = DeepResearchGraph(
        max_iterations=3,
        agents={
            role: object()
            for role in ("architect", "scout", "data_analyst", "wizard", "critic", "writer")
        },
        checkpoint_service=None,
    )

    async def first_event():
        agen = graph.run(
            "topic", "session-1",
            search_web=True,
            search_local=True,
            kb_name="demo",
        )
        try:
            return await agen.__anext__()
        finally:
            await agen.aclose()

    event = asyncio.run(first_event())
    assert captured.get("kb_name") == "demo"
    assert event["type"] == "research_start"
