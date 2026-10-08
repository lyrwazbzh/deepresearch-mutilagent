from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.agent.tools.web import (
    ReadWebpageAdapter,
    RenderPageAdapter,
    SSRFGuard,
    SearchHit,
    SearchWebAdapter,
    canonicalize_url,
)
from src.agent.identity import content_hash
from src.agent.runtime.tool_runtime import ToolExecutionContext
from src.agent.task_service import CreateTaskRequest


class Token:
    def is_cancelled(self):
        return False


class Search:
    def __init__(self, name, hits):
        self.name = name
        self.hits = hits
        self.calls = 0

    def search(self, query, **options):
        self.calls += 1
        return self.hits


class Page:
    name = "fake-page"

    def __init__(self, text):
        self.text = text

    def read(self, url):
        return "Title", self.text, None


class Renderer:
    def __init__(self):
        self.guarded = []

    def render(self, url, *, wait_until, request_guard):
        self.guarded.append(request_guard("https://cdn.example.test/app.js"))
        return "Dynamic", "rendered content"


def context(task_id, inline_budget=32_000):
    return ToolExecutionContext(
        tenant_id="tenant_test",
        user_id="user_test",
        task_id=task_id,
        thread_id="thread",
        node_run_id="node",
        cancellation_token=Token(),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
        inline_budget=inline_budget,
        operation_key="f" * 64,
    )


def public_guard():
    return SSRFGuard(resolver=lambda _host, _port: ["93.184.216.34"])


def test_search_normalization_provider_route_and_exact_dedup() -> None:
    first = Search(
        "web",
        [
            SearchHit("A", "https://Example.test/a?utm_source=x&b=2", "10.1000/ABC", "web"),
            SearchHit("B", "https://other.test/b", "doi 10.1000/abc", "web"),
        ],
    )
    paper = Search("paper", [SearchHit("P", "https://arxiv.org/abs/1234.5678", "", "paper")])
    adapter = SearchWebAdapter([first], [paper])
    web = adapter.execute(None, {"query": "q", "source": "web", "max_results": 5})
    academic = adapter.execute(None, {"query": "q", "source": "paper", "max_results": 5})
    assert len(web.data["hits"]) == 1
    assert web.data["hits"][0]["canonical_url"] == "https://example.test/a?b=2"
    assert academic.data["hits"][0]["source"] == "paper"
    assert first.calls == paper.calls == 1


def test_read_short_inline_long_workspace_and_render_is_explicit(make_container) -> None:
    container, _ = make_container({})
    task_id = container.task_service.create(
        "tenant_test", "user_test", "web", CreateTaskRequest(query="q")
    ).projection.task_id
    guard = public_guard()
    short = ReadWebpageAdapter([Page("short")], guard, container.workspace_service).execute(
        context(task_id), {"url": "https://example.test/page", "page_index": 0, "page_size_chars": 100}
    )
    assert short.data["content"] == "short"
    assert short.references[0].content_hash == content_hash(short.data)

    long = ReadWebpageAdapter([Page("x" * 500)], guard, container.workspace_service).execute(
        context(task_id, inline_budget=10),
        {"url": "https://example.test/long", "page_index": 0, "page_size_chars": 100},
    )
    assert long.data is None
    assert long.references[0].workspace_path
    assert container.workspace_service.read(
        "tenant_test", task_id, long.references[0].workspace_path
    ) == b"x" * 500

    renderer = Renderer()
    rendered = RenderPageAdapter(renderer, guard, container.workspace_service).execute(
        context(task_id),
        {
            "url": "https://example.test/dynamic",
            "page_index": 0,
            "page_size_chars": 100,
            "wait_until": "domcontentloaded",
        },
    )
    assert rendered.data["rendered"] is True
    assert renderer.guarded == ["https://cdn.example.test/app.js"]


def test_ssrf_and_canonical_url_guards() -> None:
    guard = SSRFGuard(resolver=lambda _host, _port: ["127.0.0.1"])
    with pytest.raises(PermissionError):
        guard.validate("http://localhost/admin")
    with pytest.raises(ValueError):
        canonicalize_url("file:///etc/passwd")
