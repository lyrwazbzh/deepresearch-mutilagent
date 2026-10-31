# DeepResearch Multi-Agent development guide

The repository combines an online multi-agent research application with its Qwen2.5 tool-routing
training and serving pipeline. Start with [README.md](README.md) and
[docs/architecture.md](docs/architecture.md).

## Main components

- `backend/app/service/deep_research_v2/`: six-agent AsyncIO research state machine.
- `backend/app/service/retrieval/`: Milvus + Elasticsearch/BM25 hybrid retrieval.
- `backend/app/service/tool_routing/`: versioned router client, gray rollout and rule fallback.
- `backend/app/service/resilience/`: retry, circuit breaker, timeout, idempotency and DLQ.
- `backend/app/service/research_memory/`: Fact/Experience memory.
- `backend/evals/`: deterministic metrics, five-dimension judge and bootstrap statistics.
- `training/`: data synthesis, QLoRA, evaluation, release packaging and inference.
- `frontend/`: React 19 research UI.

## Local commands

```bash
docker compose up -d postgres redis etcd minio milvus elasticsearch backend
cd frontend && npm install --legacy-peer-deps && npm run dev
cd training && python scripts/sync_tool_schema.py --source ../contracts/deepresearch_tools.v1.json --check
```

The online V2 workflow is implemented only by the hand-written AsyncIO state machine. Do not add
a second orchestration runtime without an explicit architecture decision and matching SSE,
cancellation, checkpoint, retry and evaluation behavior.
