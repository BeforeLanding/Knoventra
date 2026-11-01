# Knoventra backend (MVP, week 1: permission-aware retrieval)

Implemented: schema + Alembic migration, AccessScope (fail-closed), Markdown/PDF chunking with page and
section, dense + sparse (`sparsevec`) hybrid recall with RRF, atomic revision publish, login, `/debug/search`,
admin upload/publish. Not implemented yet: rerank/evidence gate, LangGraph agent, SSE chat, escalation, frontend, eval.

```bash
docker compose -f deploy/compose.yaml up -d
cd backend && uv sync
uv run alembic upgrade head
uv run python -m knoventra.db.seed     # KNV_ENCODER=bge-m3 needs `uv sync --extra ml`
uv run uvicorn knoventra.main:app
uv run pytest                          # uses a throwaway `knoventra_test` database and an offline hash encoder
```

Demo accounts and fictional documents: `data/demo/manifest.json` (all demo passwords are fictional).
