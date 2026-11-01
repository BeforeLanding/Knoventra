# Knoventra backend (MVP)

Implemented: week 1 retrieval (schema, AccessScope, chunking, hybrid recall, publish) and week 2 agent (LangGraph, cite_check, SSE chat, interrupt-confirmed escalation, evidence preview). Eval set and runner exist; the web frontend lives in `../web`. Not yet: evaluation report. Real BGE-M3 and a real LLM are NOT yet exercised; tests use an offline hash encoder and a scripted model.

```bash
docker compose -f deploy/compose.yaml up -d
cd backend && uv sync
uv run alembic upgrade head
uv run python -m knoventra.db.seed     # KNV_ENCODER=bge-m3 needs `uv sync --extra ml`
uv run uvicorn knoventra.main:app
uv run pytest                          # uses a throwaway `knoventra_test` database and an offline hash encoder
```

Demo accounts and fictional documents: `data/demo/manifest.json` (all demo passwords are fictional).

## Evaluation
`eval/cases.jsonl` (60 cases, regenerate with `eval/build_cases.py`): 20 dev (tuning) / 40 holdout (final only).
`uv run python eval/run_eval.py --split dev` runs retrieval metrics without an LLM; ablation flags
`--no-sparse --no-opencc --rerank`. Holdout needs `--confirm-final`. `--mode agent` needs `KNV_LLM_*`.
No numbers have been produced yet with the real BGE-M3 / LLM.
