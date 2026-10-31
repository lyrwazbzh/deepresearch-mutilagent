"""FastAPI middleware for request correlation, access logs, and HTTP metrics."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .context import bind_context, new_request_context
from .metrics import ApplicationMetrics, application_metrics
from .tracing import span


REQUEST_ID_HEADER: Final = "X-Request-ID"
SESSION_ID_HEADER: Final = "X-Session-ID"
RESEARCH_ID_HEADER: Final = "X-Research-ID"
TRACE_ID_HEADER: Final = "X-Trace-ID"
logger = logging.getLogger(__name__)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Bind correlation IDs and record one bounded HTTP observation per request."""

    def __init__(self, app, *, metrics: ApplicationMetrics = application_metrics) -> None:
        super().__init__(app)
        self.metrics = metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        context = new_request_context(
            request_id=request.headers.get(REQUEST_ID_HEADER),
            session_id=request.headers.get(SESSION_ID_HEADER),
            research_id=request.headers.get(RESEARCH_ID_HEADER),
        )
        started_at = perf_counter()
        status_code = 500

        with bind_context(context):
            with span(
                "http.request",
                attributes={"http.method": request.method, "http.path": request.url.path},
            ) as trace:
                try:
                    response = await call_next(request)
                    status_code = response.status_code
                    response.headers[REQUEST_ID_HEADER] = context.request_id
                    if context.research_id:
                        response.headers[RESEARCH_ID_HEADER] = context.research_id
                    if trace.trace_id:
                        response.headers[TRACE_ID_HEADER] = trace.trace_id
                    return response
                except Exception:
                    logger.exception(
                        "HTTP request failed",
                        extra={"event": "http.request.failed"},
                    )
                    raise
                finally:
                    duration = perf_counter() - started_at
                    route = request.scope.get("route")
                    route_path = getattr(route, "path", "unmatched")
                    self.metrics.observe_http(
                        method=request.method,
                        route=route_path,
                        status=status_code,
                        duration=duration,
                    )
                    trace.update(
                        metadata={
                            "http.route": route_path,
                            "http.status_code": status_code,
                            "duration_ms": round(duration * 1000, 3),
                        },
                    )
                    logger.info(
                        "HTTP request completed",
                        extra={
                            "event": "http.request.completed",
                            "details": {
                                "method": request.method,
                                "route": route_path,
                                "status": status_code,
                                "duration_ms": round(duration * 1000, 3),
                            },
                        },
                    )
