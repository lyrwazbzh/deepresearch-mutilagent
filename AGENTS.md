# Repository guidance

## Architecture

- `backend/` is the online FastAPI application. DeepResearch V2 has one execution runtime:
  `service/deep_research_v2/graph.py::_run_state_machine`.
- `training/` owns synthesis, dataset construction, QLoRA, offline evaluation, packaging and
  the internal `/v1/tool-routing` service.
- `contracts/deepresearch_tools.v1.json` is the canonical tool schema. After changing it,
  run `training/scripts/sync_tool_schema.py` and update both Python contract models.
- Research completion and Critic retry control belong to the AsyncIO state machine, not the
  fine-tuned model.

## Required checks

```bash
cd backend && ruff check app evals scripts && pytest tests -q
cd training && ruff check src scripts tests && pytest tests -q
cd frontend && npm run lint && npm run typecheck && npm test
```

## Safety

- Never commit credentials, DSNs, generated datasets, model weights or release adapters.
- Text2SQL must use a server-side read-only profile; never accept an arbitrary client DSN.
- Preserve tenant filters on PostgreSQL, Milvus and Elasticsearch access.
- Do not report dataset size or model gains without generated manifests and evaluation reports.
