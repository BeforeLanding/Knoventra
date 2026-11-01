# Knoventra backend (MVP)

Implemented: week 1 retrieval (schema, AccessScope, chunking, hybrid recall, publish) and week 2 agent (LangGraph, cite_check, SSE chat, interrupt-confirmed escalation, evidence preview). Not yet: frontend, eval set/report. Real BGE-M3 and a real LLM are NOT yet exercised; tests use an offline hash encoder and a scripted model.

```bash
docker compose -f deploy/compose.yaml up -d
cd backend && uv sync
uv run alembic upgrade head
uv run python -m knoventra.db.seed     # KNV_ENCODER=bge-m3 needs `uv sync --extra ml`
uv run uvicorn knoventra.main:app
uv run pytest                          # uses a throwaway `knoventra_test` database and an offline hash encoder
```

Demo accounts and fictional documents: `data/demo/manifest.json` (all demo passwords are fictional).
