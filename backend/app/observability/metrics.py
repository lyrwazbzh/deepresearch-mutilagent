"""Low-cardinality Prometheus metrics for HTTP and research workloads."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response


class ApplicationMetrics:
    """Own all application instruments and their bounded label sets."""

    def __init__(self, *, registry: CollectorRegistry = REGISTRY) -> None:
        self.registry = registry
        self.http_requests = Counter(
            "industry_http_requests_total",
            "HTTP requests handled by the API.",
            ("method", "route", "status"),
            registry=registry,
        )
        self.http_duration = Histogram(
            "industry_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            ("method", "route"),
            registry=registry,
        )
        self.research_runs = Counter(
            "industry_research_runs_total",
            "Deep-research runs by terminal outcome.",
            ("outcome",),
            registry=registry,
        )
        self.research_phase_duration = Histogram(
            "industry_research_phase_duration_seconds",
            "Deep-research phase latency in seconds.",
            ("phase", "outcome"),
            registry=registry,
        )
        self.llm_calls = Counter(
            "industry_llm_calls_total",
            "LLM calls by provider, model, and outcome.",
            ("provider", "model", "outcome"),
            registry=registry,
        )
        self.llm_duration = Histogram(
            "industry_llm_request_duration_seconds",
            "LLM call latency in seconds.",
            ("provider", "model"),
            registry=registry,
        )
        self.llm_tokens = Counter(
            "industry_llm_tokens_total",
            "LLM token consumption.",
            ("provider", "model", "direction"),
            registry=registry,
        )
        self.retrieval_duration = Histogram(
            "industry_retrieval_duration_seconds",
            "Retrieval latency in seconds.",
            ("source", "outcome"),
            registry=registry,
        )
        self.tool_calls = Counter(
            "industry_tool_calls_total",
            "Research tool calls by type and outcome.",
            ("tool", "outcome"),
            registry=registry,
        )
        self.tool_duration = Histogram(
            "industry_tool_duration_seconds",
            "Research tool call latency in seconds.",
            ("tool", "outcome"),
            registry=registry,
        )
        self.queue_depth = Gauge(
            "industry_research_queue_depth",
            "Number of research jobs waiting to run.",
            registry=registry,
        )
        self.resilience_retries = Counter(
            "industry_resilience_retries_total",
            "Retried dependency calls.",
            ("component", "provider"),
            registry=registry,
        )
        self.resilience_fallbacks = Counter(
            "industry_resilience_fallbacks_total",
            "Successful dependency fallback calls.",
            ("component", "provider"),
            registry=registry,
        )
        self.resilience_circuit_rejections = Counter(
            "industry_resilience_circuit_rejections_total",
            "Calls rejected by an open circuit.",
            ("component", "provider"),
            registry=registry,
        )
        self.resilience_dlq = Counter(
            "industry_resilience_dlq_total",
            "Failed calls written to the dead-letter queue.",
            ("component",),
            registry=registry,
        )
        self.idempotency_hits = Counter(
            "industry_idempotency_hits_total",
            "Completed results returned from the idempotency store.",
            ("component",),
            registry=registry,
        )
        self.tool_router_decisions = Counter(
            "industry_tool_router_decisions_total",
            "Tool-routing decisions by selected path and outcome.",
            ("path", "outcome"),
            registry=registry,
        )
        self.memory_operations = Counter(
            "industry_research_memory_operations_total",
            "Fact/Experience memory operations.",
            ("operation", "outcome"),
            registry=registry,
        )
        self.memory_retrieval_hits = Histogram(
            "industry_research_memory_retrieval_hits",
            "Number of memories returned to planning.",
            buckets=(0, 1, 2, 3, 5, 8, 13, 20),
            registry=registry,
        )

    def observe_http(self, *, method: str, route: str, status: int, duration: float) -> None:
        normalized_method = method.upper()
        normalized_route = route if route.startswith("/") else "unmatched"
        self.http_requests.labels(normalized_method, normalized_route, str(status)).inc()
        self.http_duration.labels(normalized_method, normalized_route).observe(max(duration, 0.0))


application_metrics = ApplicationMetrics()


def metrics_response(registry: CollectorRegistry = REGISTRY) -> Response:
    """Render a Prometheus text exposition response."""

    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
