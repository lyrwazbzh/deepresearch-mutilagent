# DeepResearch Workbench

Phase 11 is a client-rendered React + strict TypeScript application. It uses React Router for URL
identity, TanStack Query as the only server-state cache, one fetch-based HTTP client, and a
separate authenticated fetch-stream SSE lifecycle. Access tokens remain in memory; refresh
credentials remain in the backend-issued HttpOnly cookie.

## Development

```powershell
pnpm install
pnpm dev
```

The default development server is `http://localhost:5173` and proxies `/api` to
`http://127.0.0.1:8000`. Set `VITE_API_BASE_URL` only when testing cross-origin deployment.

## Verification

```powershell
pnpm quality
pnpm e2e
```

Real browser acceptance requires `E2E_EMAIL`, `E2E_PASSWORD`, and `E2E_TENANT_ID`. The complete
resource test also accepts `E2E_FIXTURE_TASK_ID` and `E2E_FIXTURE_THREAD_ID`; prepare those values
with `src/test/fixtures/browser_fixture.py` against a dedicated local acceptance identity. The
fixture reuses the configured PostgreSQL, MinIO and Neo4j stores and is not production logic.
Pending-write refresh acceptance additionally uses `E2E_PENDING_TASK_ID` and
`E2E_PENDING_THREAD_ID`; pass its Task to the same fixture with `--pending-task-id`.
