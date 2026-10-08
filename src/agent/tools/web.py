from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal, Protocol

import httpx
from opentelemetry.trace import SpanKind
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from src.agent.identity import canonical_json, sha256_hex
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolExecutionContext,
    ToolReference,
    ToolResult,
)
from src.agent.backends.observability import add_span_event, current_span
from src.agent.backends.workspace import WorkspaceService

TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class SSRFDeniedError(PermissionError):
    reason_code = "ssrf_denied"


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute http/https URL")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower().startswith("utm_") or key.lower() in TRACKING_KEYS:
            continue
        query.append((key, value))
    query.sort()
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(
        (scheme, netloc, path, urllib.parse.urlencode(query, doseq=True), "")
    )


class SSRFGuard:
    def __init__(
        self,
        resolver: Callable[[str, int], list[str]] | None = None,
        *,
        allowed_ports: frozenset[int] = frozenset({80, 443}),
    ) -> None:
        self.resolver = resolver or self._resolve
        self.allowed_ports = allowed_ports

    @staticmethod
    def _resolve(host: str, port: int) -> list[str]:
        return sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            }
        )

    def validate(self, url: str) -> str:
        normalized = canonicalize_url(url)
        parsed = urllib.parse.urlsplit(normalized)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in self.allowed_ports:
            raise SSRFDeniedError("URL port is not allowed")
        addresses = self.resolver(parsed.hostname, port)
        if not addresses:
            raise SSRFDeniedError("URL host did not resolve")
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if not address.is_global:
                raise SSRFDeniedError("URL resolves to a non-public address")
        return normalized


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    source: str
    published_at: datetime | None = None
    doi: str | None = None
    arxiv_id: str | None = None


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, **options: Any) -> list[SearchHit]: ...


class PageProvider(Protocol):
    name: str

    def read(self, url: str) -> tuple[str | None, str, datetime | None]: ...


class TavilySearchProvider:
    name = "tavily"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)

    def search(self, query: str, **options: Any) -> list[SearchHit]:
        if not self.api_key:
            raise PermissionError("Tavily credential is not configured")
        response = self.client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": options.get("max_results", 5),
                "include_domains": options.get("domains") or [],
            },
        )
        response.raise_for_status()
        return [
            SearchHit(
                title=str(item.get("title") or ""),
                url=str(item["url"]),
                snippet=str(item.get("content") or "")[:2000],
                source="web",
                published_at=_parse_datetime(item.get("published_date")),
            )
            for item in response.json().get("results", [])
        ]


class ArxivSearchProvider:
    name = "arxiv"
    endpoint = "https://export.arxiv.org/api/query"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)

    def search(self, query: str, **options: Any) -> list[SearchHit]:
        response = self.client.get(
            self.endpoint,
            params={
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": options.get("max_results", 5),
            },
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        atom = {"a": "http://www.w3.org/2005/Atom"}
        hits: list[SearchHit] = []
        for entry in root.findall("a:entry", atom):
            identity = (entry.findtext("a:id", default="", namespaces=atom)).rstrip("/")
            arxiv_id = identity.rsplit("/", 1)[-1]
            hits.append(
                SearchHit(
                    title=" ".join(
                        entry.findtext("a:title", default="", namespaces=atom).split()
                    ),
                    url=identity,
                    snippet=" ".join(
                        entry.findtext("a:summary", default="", namespaces=atom).split()
                    )[:2000],
                    source="paper",
                    published_at=_parse_datetime(
                        entry.findtext("a:published", default=None, namespaces=atom)
                    ),
                    arxiv_id=arxiv_id,
                )
            )
        return hits


class JinaSearchProvider:
    name = "jina-search"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)

    def search(self, query: str, **options: Any) -> list[SearchHit]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.client.get(
            f"https://s.jina.ai/{urllib.parse.quote(query)}",
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", payload.get("results", []))
        return [
            SearchHit(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or item.get("link")),
                snippet=str(item.get("description") or item.get("content") or "")[:2000],
                source="web",
            )
            for item in items[: int(options.get("max_results", 5))]
            if item.get("url") or item.get("link")
        ]


class JinaReaderProvider:
    name = "jina-reader"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def read(self, url: str) -> tuple[str | None, str, datetime | None]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = self.client.get(f"https://r.jina.ai/{url}", headers=headers)
        response.raise_for_status()
        text = response.text
        title = None
        if text.startswith("Title:"):
            first, _, body = text.partition("\n")
            title = first.removeprefix("Title:").strip() or None
            text = body
        return title, text.strip(), None


class HttpPageProvider:
    name = "http-readability"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def read(self, url: str) -> tuple[str | None, str, datetime | None]:
        response = self.client.get(url, headers={"User-Agent": "DeepResearch/1.0"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for element in soup(["script", "style", "noscript"]):
            element.decompose()
        content = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        return soup.title.string.strip() if soup.title and soup.title.string else None, content, None


class BrowserRenderer(Protocol):
    def render(
        self,
        url: str,
        *,
        wait_until: str,
        request_guard: Callable[[str], str],
    ) -> tuple[str | None, str]: ...


class PlaywrightBrowserRenderer:
    def render(
        self,
        url: str,
        *,
        wait_until: str,
        request_guard: Callable[[str], str],
    ) -> tuple[str | None, str]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("playwright is required for render_page") from exc
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(storage_state=None)
            page = context.new_page()

            def route_request(route: Any) -> None:
                try:
                    request_guard(route.request.url)
                except Exception:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", route_request)
            try:
                page.goto(url, wait_until=wait_until, timeout=60_000)
                title = page.title() or None
                content = page.locator("body").inner_text(timeout=10_000)
                return title, content
            finally:
                context.close()
                browser.close()


class SearchWebArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=400)
    source: Literal["web", "paper"] = "web"
    max_results: int = Field(default=5, ge=1, le=20)
    domains: list[str] | None = None
    published_after: date | None = None
    published_before: date | None = None


class ReadWebpageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    page_index: int = Field(default=0, ge=0)
    page_size_chars: int = Field(default=12_000, ge=1, le=40_000)


class RenderPageArgs(ReadWebpageArgs):
    wait_until: Literal["domcontentloaded", "networkidle"] = "domcontentloaded"


class SearchWebAdapter:
    def __init__(
        self,
        web_providers: list[SearchProvider],
        paper_providers: list[SearchProvider],
    ) -> None:
        self.web_providers = web_providers
        self.paper_providers = paper_providers

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        providers = self.paper_providers if arguments.get("source", "web") == "paper" else self.web_providers
        failures: list[str] = []
        for attempt_no, provider in enumerate(providers, start=1):
            if attempt_no > 1:
                add_span_event(
                    "tool.fallback",
                    {
                        "from_provider": providers[attempt_no - 2].name,
                        "to_provider": provider.name,
                        "reason_code": "provider_failed",
                    },
                )
            try:
                with current_span(
                    "web.search",
                    kind=SpanKind.CLIENT,
                    attributes={
                        "client.operation": "search",
                        "server.address": provider.name,
                    },
                ):
                    options = {key: value for key, value in arguments.items() if key != "query"}
                    raw = provider.search(arguments["query"], **options)
                published_after = arguments.get("published_after")
                published_before = arguments.get("published_before")
                if published_after:
                    raw = [
                        hit
                        for hit in raw
                        if hit.published_at is None
                        or hit.published_at.date() >= published_after
                    ]
                if published_before:
                    raw = [
                        hit
                        for hit in raw
                        if hit.published_at is None
                        or hit.published_at.date() <= published_before
                    ]
                hits = deduplicate_hits(raw)
                return ToolResult(
                    status="success",
                    summary=f"{len(hits)} normalized search candidates from {provider.name}",
                    data={
                        "hits": [
                            {
                                "title": hit.title,
                                "url": hit.url,
                                "canonical_url": canonicalize_url(hit.url),
                                "snippet": hit.snippet[:2000],
                                "source": hit.source,
                                "published_at": hit.published_at.isoformat()
                                if hit.published_at
                                else None,
                                "doi": hit.doi,
                                "arxiv_id": hit.arxiv_id,
                            }
                            for hit in hits[: arguments.get("max_results", 5)]
                        ]
                    },
                )
            except Exception as exc:
                failures.append(f"{provider.name}:{type(exc).__name__}")
                add_span_event(
                    "tool.retry",
                    {
                        "attempt_no": attempt_no,
                        "provider": provider.name,
                        "error.type": type(exc).__name__,
                    },
                )
        raise ConnectionError("web provider chain exhausted: " + ",".join(failures))


class ReadWebpageAdapter:
    def __init__(
        self,
        providers: list[PageProvider],
        guard: SSRFGuard,
        workspace: WorkspaceService,
    ) -> None:
        self.providers = providers
        self.guard = guard
        self.workspace = workspace

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        canonical_url = self.guard.validate(arguments["url"])
        failures: list[str] = []
        for attempt_no, provider in enumerate(self.providers, start=1):
            if attempt_no > 1:
                add_span_event(
                    "tool.fallback",
                    {
                        "from_provider": self.providers[attempt_no - 2].name,
                        "to_provider": provider.name,
                        "reason_code": "provider_failed",
                    },
                )
            try:
                with current_span(
                    "web.read",
                    kind=SpanKind.CLIENT,
                    attributes={
                        "client.operation": "read",
                        "server.address": provider.name,
                    },
                ):
                    title, content, published_at = provider.read(canonical_url)
                if not content.strip():
                    raise ValueError("parse_failed")
                return _page_result(
                    context=context,
                    workspace=self.workspace,
                    canonical_url=canonical_url,
                    title=title,
                    content=content,
                    published_at=published_at,
                    page_index=arguments.get("page_index", 0),
                    page_size=arguments.get("page_size_chars", 12_000),
                    rendered=False,
                )
            except Exception as exc:
                failures.append(f"{provider.name}:{type(exc).__name__}")
                add_span_event(
                    "tool.retry",
                    {
                        "attempt_no": attempt_no,
                        "provider": provider.name,
                        "error.type": type(exc).__name__,
                    },
                )
        return ToolResult(
            status="error",
            error={
                "type": "tool_error",
                "code": "parse_failed",
                "message": "Static page extraction failed; render_page may be called explicitly. "
                + ",".join(failures),
                "retryable": False,
            },
        )


class RenderPageAdapter:
    def __init__(
        self,
        renderer: BrowserRenderer,
        guard: SSRFGuard,
        workspace: WorkspaceService,
    ) -> None:
        self.renderer = renderer
        self.guard = guard
        self.workspace = workspace

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        canonical_url = self.guard.validate(arguments["url"])
        title, content = self.renderer.render(
            canonical_url,
            wait_until=arguments.get("wait_until", "domcontentloaded"),
            request_guard=self.guard.validate,
        )
        if not content.strip():
            raise ValueError("parse_failed")
        return _page_result(
            context=context,
            workspace=self.workspace,
            canonical_url=canonical_url,
            title=title,
            content=content,
            published_at=None,
            page_index=arguments.get("page_index", 0),
            page_size=arguments.get("page_size_chars", 12_000),
            rendered=True,
        )


def _page_result(
    *,
    context: ToolExecutionContext,
    workspace: WorkspaceService,
    canonical_url: str,
    title: str | None,
    content: str,
    published_at: datetime | None,
    page_index: int,
    page_size: int,
    rendered: bool,
) -> ToolResult:
    normalized = "\n".join(line.rstrip() for line in content.replace("\r\n", "\n").split("\n")).strip()
    start = page_index * page_size
    page = normalized[start : start + page_size]
    if not page:
        raise ValueError("page_index is beyond extracted content")
    has_more = start + page_size < len(normalized)
    retrieved_at = datetime.now(timezone.utc)
    inline = {
        "title": title,
        "content": page,
        "page_index": page_index,
        "page_size_chars": page_size,
        "has_more": has_more,
        "next_page_index": page_index + 1 if has_more else None,
        "rendered": rendered,
    }
    common: dict[str, Any] = {
        "source_uri": canonical_url,
        "canonical_url": canonical_url,
        "published_at": published_at,
        "retrieved_at": retrieved_at,
    }
    encoded = canonical_json(inline)
    if len(encoded) <= context.inline_budget and len(normalized) <= page_size:
        digest = sha256_hex(encoded)
        return ToolResult(
            status="success",
            summary=page[:500],
            data=inline,
            references=[ToolReference(**common, content_hash=digest)],
            truncated=has_more,
            next_cursor=str(page_index + 1) if has_more else None,
        )
    body = normalized.encode("utf-8")
    digest = sha256_hex(body)
    path_hash = sha256_hex(canonical_url)[:20]
    workspace_file = workspace.publish_bytes(
        tenant_id=context.tenant_id,
        task_id=context.task_id,
        path=(
            f"files/web/{path_hash}/"
            f"{'rendered' if rendered else 'static'}/page-{page_index}.txt"
        ),
        data=body,
        mime_type="text/plain; charset=utf-8",
        operation_key=context.operation_key,
    )
    return ToolResult(
        status="success",
        summary=page[:500],
        references=[
            ToolReference(
                **common,
                workspace_path=workspace_file.workspace_path,
                content_hash=digest,
                mime_type=workspace_file.mime_type,
                size=workspace_file.size,
            )
        ],
        truncated=False,
    )


def deduplicate_hits(hits: list[SearchHit]) -> list[SearchHit]:
    seen: set[str] = set()
    output: list[SearchHit] = []
    for hit in hits:
        doi = (hit.doi or _extract_doi(hit.url + " " + hit.snippet) or "").lower()
        arxiv = (hit.arxiv_id or _extract_arxiv(hit.url) or "").lower()
        key = f"doi:{doi}" if doi else f"arxiv:{arxiv}" if arxiv else f"url:{canonicalize_url(hit.url)}"
        if key in seen:
            continue
        seen.add(key)
        output.append(hit)
    return output


def _extract_doi(value: str) -> str | None:
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", value, re.IGNORECASE)
    return match.group(0).rstrip(".,)") if match else None


def _extract_arxiv(value: str) -> str | None:
    match = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|arXiv:)([\w.\-/]+)", value, re.IGNORECASE)
    return match.group(1).removesuffix(".pdf") if match else None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def tool_definitions(
    *,
    workspace: WorkspaceService,
    guard: SSRFGuard | None = None,
    web_search: list[SearchProvider] | None = None,
    paper_search: list[SearchProvider] | None = None,
    readers: list[PageProvider] | None = None,
    renderer: BrowserRenderer | None = None,
    tavily_api_key: str | None = None,
    jina_api_key: str | None = None,
) -> list[ToolDefinition]:
    guard = guard or SSRFGuard()
    search_adapter = SearchWebAdapter(
        web_search
        or [
            TavilySearchProvider(api_key=tavily_api_key),
            JinaSearchProvider(api_key=jina_api_key),
        ],
        paper_search or [ArxivSearchProvider(), JinaSearchProvider(api_key=jina_api_key)],
    )
    return [
        ToolDefinition(
            name="search_web",
            args_schema=SearchWebArgs,
            adapter=search_adapter,
            search_call=True,
            effect_target=lambda args: sha256_hex(canonical_json(args))[:32],
        ),
        ToolDefinition(
            name="read_webpage",
            args_schema=ReadWebpageArgs,
            adapter=ReadWebpageAdapter(
                readers or [JinaReaderProvider(api_key=jina_api_key), HttpPageProvider()],
                guard,
                workspace,
            ),
            evidence_eligible=True,
            effect_target=lambda args: f"{canonicalize_url(args['url'])}:page:{args.get('page_index', 0)}",
        ),
        ToolDefinition(
            name="render_page",
            args_schema=RenderPageArgs,
            adapter=RenderPageAdapter(renderer or PlaywrightBrowserRenderer(), guard, workspace),
            evidence_eligible=True,
            effect_target=lambda args: (
                f"{canonicalize_url(args['url'])}:page:{args.get('page_index', 0)}:"
                f"wait:{args.get('wait_until', 'domcontentloaded')}:renderer:v1"
            ),
        ),
    ]
