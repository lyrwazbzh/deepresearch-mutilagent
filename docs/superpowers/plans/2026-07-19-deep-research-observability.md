# Deep Research Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build correlated JSON logs, OpenTelemetry/Langfuse traces, Prometheus metrics, durable research event history, monitoring infrastructure, and a refresh-safe research diagnostics timeline.

**Architecture:** A request-scoped context carries request, research, run, trace, and span identifiers through FastAPI and the research pipeline. Instrumentation is isolated behind no-op-safe observability services; PostgreSQL stores append-only business events, while logs, metrics, and traces are exported independently and must never block report generation.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/PostgreSQL, OpenTelemetry, Langfuse, prometheus-client, React 19, TypeScript, Vitest, Playwright, Elasticsearch/Kibana, Filebeat, Prometheus, Grafana, Alertmanager.

---

## File Map

### Backend application

- Create `backend/app/observability/context.py`: typed context variables and ID lifecycle.
- Create `backend/app/observability/logging.py`: JSON formatter, redaction, rotating-file configuration.
- Create `backend/app/observability/metrics.py`: bounded-label Prometheus instruments and helpers.
- Create `backend/app/observability/tracing.py`: OpenTelemetry/Langfuse setup and no-op-safe spans.
- Create `backend/app/observability/middleware.py`: FastAPI request context, HTTP metrics, correlation headers.
- Create `backend/app/observability/events.py`: event sanitization and recorder facade.
- Create `backend/app/observability/__init__.py`: stable public API.
- Create `backend/app/models/observability.py`: `ResearchRun` and `ResearchEvent` models.
- Create `backend/app/service/research_observability_service.py`: run/event transactions and queries.
- Create `backend/app/router/observability_router.py`: authenticated run/event/timeline endpoints.
- Modify `backend/app/app_main.py`: initialize and shut down observability, register middleware, metrics and router.
- Modify `backend/app/models/__init__.py`: export new models.
- Modify `backend/app/service/deep_research_v2/service.py`: create/finalize runs and record streamed events.
- Modify `backend/app/service/deep_research_v2/graph.py`: phase, checkpoint and completion spans/events.
- Modify `backend/app/service/deep_research_v2/agents/base.py`: LLM metrics and generation spans.
- Modify `backend/app/service/deep_research_v2/agents/scout.py`: web/Milvus retrieval spans and metrics.
- Modify `backend/app/service/deep_research_v2/agents/wizard.py`: code-tool spans and metrics.
- Modify `backend/app/requirements.txt` and `backend/requirements.txt`: pinned observability dependencies.
- Create `backend/migrations/20260719_research_observability.sql`: idempotent schema migration.

### Tests

- Create `backend/tests/observability/test_context.py`.
- Create `backend/tests/observability/test_logging.py`.
- Create `backend/tests/observability/test_metrics.py`.
- Create `backend/tests/observability/test_tracing.py`.
- Create `backend/tests/service/test_research_observability_service.py`.
- Create `backend/tests/router/test_observability_router.py`.
- Extend `backend/tests/service/test_graph_outline_checkpoint.py`.
- Extend `backend/tests/router/test_research_outline_approval.py`.

### Frontend

- Modify `frontend/src/api/session.ts`: run, event and timeline types/API.
- Create `frontend/src/pages/chat/component/research-detail/research-diagnostics.tsx`.
- Create `frontend/src/pages/chat/component/research-detail/research-diagnostics.module.scss`.
- Modify `frontend/src/pages/chat/component/research-detail/index.tsx`: diagnostics tab.
- Modify `frontend/src/pages/chat/index.tsx`: load and refresh timeline by session.
- Create `frontend/src/pages/chat/component/research-detail/research-diagnostics.test.tsx`.
- Extend `frontend/src/pages/chat/deep-research-integration.test.tsx`.
- Extend `frontend/e2e/deep-research-outline-approval.spec.ts`.

### Infrastructure and documentation

- Create `observability/docker-compose.observability.yml`.
- Create `observability/filebeat/filebeat.yml`.
- Create `observability/prometheus/prometheus.yml`.
- Create `observability/prometheus/alerts.yml`.
- Create `observability/alertmanager/alertmanager.yml`.
- Create `observability/grafana/provisioning/datasources/datasources.yml`.
- Create `observability/grafana/provisioning/dashboards/dashboards.yml`.
- Create `observability/grafana/dashboards/deep-research.json`.
- Create `observability/README.md`.
- Modify `.env.example` or create it if absent with non-secret defaults.

## Task 1: Correlation Context and JSON Logging

**Files:** context, logging, app bootstrap and their tests.

- [ ] Write failing tests proving context isolation across concurrent async tasks, safe ID generation, JSON fields, exception serialization, recursive secret redaction, and no leakage after context reset.
- [ ] Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/observability/test_context.py backend/tests/observability/test_logging.py -q
```

Expected: collection/import failure because `observability.context` and `observability.logging` do not exist.

- [ ] Implement immutable `ObservabilityContext`, `bind_context`, `reset_context`, `current_context`, `safe_identifier`, `JsonFormatter`, `redact`, and idempotent `configure_logging`.

Core interface:

```python
@dataclass(frozen=True)
class ObservabilityContext:
    request_id: str
    session_id: str | None = None
    research_id: str | None = None
    run_id: str | None = None

@contextmanager
def bind_context(**values: str | None) -> Iterator[ObservabilityContext]: ...

def configure_logging(*, environment: str, log_path: Path | None) -> None: ...
```

- [ ] Re-run the two tests; expected all pass.
- [ ] Run existing backend tests; expected no regression.
- [ ] Commit `feat: add correlated structured logging`.

## Task 2: HTTP Middleware and Prometheus Metrics

**Files:** metrics, middleware, app_main, dependency manifests and tests.

- [ ] Write failing tests proving `/metrics` exposition, correlation response headers, normalized FastAPI route labels, request duration recording, bounded labels, and context cleanup on exceptions.
- [ ] Run the new tests and confirm expected import/404 failures.
- [ ] Add compatible pinned dependencies:

```text
prometheus-client>=0.22,<1
opentelemetry-api>=1.38,<2
opentelemetry-sdk>=1.38,<2
opentelemetry-exporter-otlp-proto-http>=1.38,<2
langfuse>=4,<5
```

- [ ] Implement a registry module with counters/histograms/gauges and explicit label validation. Register middleware before CORS, mount `/metrics`, and return `X-Request-ID`, `X-Trace-ID`, and `X-Run-ID` where available.
- [ ] Run observability and full backend tests; expected all pass.
- [ ] Commit `feat: expose application observability metrics`.

## Task 3: Durable Research Run and Event Ledger

**Files:** SQLAlchemy models, SQL migration, service, exports and tests.

- [ ] Write failing service tests for run creation, research ID reuse, sequence uniqueness, sanitized payloads, status transitions, degraded event persistence, cursor pagination, and user scoping.
- [ ] Run the service tests and confirm failure because models/service are missing.
- [ ] Implement `ResearchRun` and `ResearchEvent` with indexes on session, research, run, status and time; enforce unique `(run_id, sequence)` and cascading delete from runs to events.
- [ ] Add an idempotent PostgreSQL migration using `CREATE TABLE IF NOT EXISTS`, guarded index creation, and no destructive alteration.
- [ ] Implement `ResearchObservabilityService` with short transactions and a payload sanitizer that replaces content-heavy fields with lengths/hashes/counts.
- [ ] Run service tests and the isolated PostgreSQL integration suite; expected pass or environment-marked skip.
- [ ] Commit `feat: persist deep research run events`.

## Task 4: Authenticated Timeline APIs

**Files:** router, schemas if needed, app registration and router tests.

- [ ] Write failing tests for:
  - `GET /research/runs?session_id=...`
  - `GET /research/runs/{run_id}`
  - `GET /research/runs/{run_id}/events`
  - `GET /research/sessions/{session_id}/timeline`
  - ownership denial, missing resources, invalid cursor and bounded limits.
- [ ] Confirm the tests fail with 404 routes.
- [ ] Implement cursor-pagination envelopes and ownership checks through `ChatSession.user_id`; never return raw full prompt/response fields.
- [ ] Register the router in `app_main.py` and verify OpenAPI generation.
- [ ] Run router and full backend tests; expected pass.
- [ ] Commit `feat: add research diagnostics APIs`.

## Task 5: OpenTelemetry and Langfuse Trace Export

**Files:** tracing module, app lifecycle, BaseAgent and tracing tests.

- [ ] Write failing tests proving disabled mode is no-op, initialization failure degrades safely, nested spans inherit context, generation attributes include model/token/latency, sensitive payload recording follows configuration, and shutdown flush is bounded.
- [ ] Confirm tests fail because tracing implementation is missing.
- [ ] Implement an OTEL-native tracing facade:

```python
@contextmanager
def span(name: str, *, kind: str = "span", attributes: Mapping[str, Scalar] | None = None): ...

@contextmanager
def generation(name: str, *, model: str, input_summary: Mapping[str, object]): ...

def record_generation_usage(span, *, input_tokens: int, output_tokens: int) -> None: ...
```

- [ ] Configure Langfuse/OTLP only when enabled and fully configured. Export failures increment `observability_export_failures_total` and never raise into research code.
- [ ] Instrument `BaseAgent.call_llm` with model, agent, latency, response length, token usage and status; retain existing return and exception behavior.
- [ ] Run tests and commit `feat: trace deep research llm calls`.

## Task 6: Research Pipeline, Retrieval and Tool Instrumentation

**Files:** service, graph, scout, wizard and existing research tests.

- [ ] Extend tests first to prove:
  - initial, approved and resumed execution use one `research_id` but distinct `run_id` values;
  - run status and events match awaiting approval/completed/failed/cancelled outcomes;
  - phase spans and events are ordered;
  - final checkpoint success precedes `REPORT_COMPLETED`;
  - event/trace backend failure does not block a final report;
  - web/Milvus/code tool calls record bounded metrics and sanitized events.
- [ ] Run targeted tests and observe expected failures.
- [ ] Add run lifecycle to `DeepResearchV2Service.research`, propagating context into `graph.run` and recording streamed event summaries.
- [ ] Add phase/checkpoint spans around existing graph boundaries without changing state-machine decisions.
- [ ] Instrument Scout web search and local retrieval, plus Wizard code execution. Do not log full documents, generated code, prompts or report text.
- [ ] Run targeted and full backend suites; expected pass.
- [ ] Commit `feat: instrument deep research workflow`.

## Task 7: Observability Infrastructure

**Files:** Compose, Filebeat, Prometheus, Grafana, Alertmanager and README.

- [ ] Add configuration validation tests or commands before services are started:

```powershell
docker compose -f docker-compose.yml -f observability/docker-compose.observability.yml config --quiet
```

Expected initially: missing file.

- [ ] Create an additive compose file with localhost-only ports and pinned compatible versions. Reuse `industry_network` and the existing Elasticsearch service; add Kibana, Filebeat, Prometheus, Grafana, Alertmanager, cAdvisor, postgres-exporter and redis-exporter.
- [ ] Provision Prometheus scrape targets for `host.docker.internal:8000/metrics`, exporters and Milvus metrics. Add recording/alert rules using bounded labels only.
- [ ] Provision Grafana data sources and a valid dashboard JSON covering API, research, LLM/RAG and dependencies.
- [ ] Configure Filebeat JSON decoding to the dedicated `industry-assistant-logs-*` data stream. Logstash remains an optional profile, not a mandatory hop.
- [ ] Validate Compose and configuration syntax, then start the profile and verify every health endpoint.
- [ ] Commit `ops: add deep research observability stack`.

## Task 8: Frontend Diagnostics Timeline

**Files:** session API, diagnostics component/styles, research-detail integration and tests.

- [ ] Write failing component tests for status summary, ordered events, duration/model/token rendering, sanitized payload behavior, trace copying, Langfuse link and degraded state.
- [ ] Write a failing integration test proving timeline restoration after page refresh and stale-session response isolation.
- [ ] Confirm the tests fail because diagnostics UI/API do not exist.
- [ ] Implement TypeScript types and API calls with abortable session-scoped loading.
- [ ] Implement a compact diagnostics tab with phase grouping, event expansion, status semantics and accessible controls. Keep existing responsive approval layout intact.
- [ ] Merge live SSE events with persisted events by `(run_id, sequence)` and reconcile from the server after completion/refresh.
- [ ] Run targeted component/integration tests and full frontend tests.
- [ ] Commit `feat: show deep research diagnostics timeline`.

## Task 9: End-to-End Verification and Documentation

**Files:** Playwright spec, observability README and any fixes proven necessary by tests.

- [ ] Extend E2E tests first for query → outline → approval → completion → refresh timeline, plus resume creating a new Run.
- [ ] Run E2E and observe expected failure before completing missing wiring.
- [ ] Implement only the missing integration wiring exposed by E2E failures.
- [ ] Run fresh verification:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests -q
Set-Location frontend
npm test
npm run test:e2e -- --reporter=list
npm run build
Set-Location ..
docker compose -f docker-compose.yml -f observability/docker-compose.observability.yml config --quiet
git diff --check
```

- [ ] Start the stack and perform one real research smoke test. Verify the same run is visible in PostgreSQL events, JSON logs, Langfuse, Prometheus and the frontend timeline; verify Redis/Milvus absence is represented as “not used”, not an error.
- [ ] Document local URLs, commands, retention, secret handling and failure diagnosis in `observability/README.md`.
- [ ] Commit `test: verify deep research observability`.

## Plan Self-Review

- Every requirement in the approved design maps to Tasks 1–9.
- All production behavior changes begin with a failing automated test.
- The public context, metrics, tracing and event interfaces are defined before their consumers.
- No task requires storing raw credentials, full documents, image Base64 or unredacted production prompts.
- Infrastructure is additive and does not repurpose the existing business Elasticsearch indexes.
- Each task produces a testable commit and preserves the two untracked research image artifacts.
