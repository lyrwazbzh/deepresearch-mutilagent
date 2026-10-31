"""FastAPI process exposing the versioned tool-routing contract."""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

from src.contracts.tool_routing import ToolRoutingRequest, ToolRoutingResponse

from .engine import ModelOutputError, ToolRouterEngine
from .telemetry import configure_telemetry


REQUESTS = Counter(
    "tool_router_requests_total", "Routing requests by outcome.", ("outcome",)
)
LATENCY = Histogram(
    "tool_router_request_duration_seconds", "Routing request latency."
)


def create_app(engine=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or ToolRouterEngine.from_environment()
        yield

    app = FastAPI(title="DeepResearch Tool Router", version="1.0", lifespan=lifespan)
    configure_telemetry(app)

    def authenticate(authorization: str | None = Header(default=None)):
        expected = os.getenv("TOOL_ROUTER_API_TOKEN", "")
        if not expected:
            return
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.get("/healthz")
    async def healthz(request: Request):
        return {"status": "ok", **request.app.state.engine.model_info()}

    @app.get("/v1/model", dependencies=[Depends(authenticate)])
    async def model_info(request: Request):
        return request.app.state.engine.model_info()

    @app.post(
        "/v1/tool-routing",
        response_model=ToolRoutingResponse,
        dependencies=[Depends(authenticate)],
    )
    async def route(payload: ToolRoutingRequest, request: Request):
        started = perf_counter()
        try:
            result = await request.app.state.engine.route(payload)
            REQUESTS.labels("success").inc()
            return result
        except ModelOutputError as exc:
            REQUESTS.labels("invalid_model_output").inc()
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception:
            REQUESTS.labels("failure").inc()
            raise
        finally:
            LATENCY.observe(perf_counter() - started)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
