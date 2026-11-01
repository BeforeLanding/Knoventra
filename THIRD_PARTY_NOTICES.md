# Third-party notices

## Ragent

- Project: Ragent AI
- Repository: https://github.com/nageoffer/ragent
- Imported branch: main
- Imported commit: c3b6157730ac1f66a312bcd286c47d3ee6aaac42
- Import date: 2026-10-26
- License: Apache License, Version 2.0; see [LICENSE](LICENSE).

Knoventra is based on Ragent and retains the imported source history and existing source notices. The original project README is preserved at [README.upstream.md](README.upstream.md).

Changes in the initial Knoventra documentation commit: replace the root README with a Knoventra introduction, preserve the upstream README separately, and add a secondary-development plan and this attribution file. No application behavior is changed by that documentation commit.

The upstream snapshot contains a root LICENSE and no root NOTICE file. This file records attribution for the import; it does not replace the upstream license or assert ownership of upstream contributions. Dependencies and bundled third-party assets remain subject to their respective licenses.

## Planned Python reimplementation

On 2026-10-27, the development plan was revised to target Python, FastAPI, LangChain and LangGraph while retaining the imported Java baseline for provenance and behavioral reference. The root README and development plan describe planned work, not an implemented Python backend. This documentation update does not change application behavior or remove upstream notices.

On 2026-11-01, the plan was narrowed to a three-week MVP (v3.0): a permission-aware knowledge Q&A service on Python, FastAPI, LangGraph and PostgreSQL / pgvector, with a new lightweight frontend that may port selected components from the upstream `frontend/`. This documentation update likewise does not change application behavior or remove upstream notices.

Any copied or modified upstream code remains subject to its applicable license and attribution requirements. New dependencies and assets will be reviewed under their respective licenses when introduced.

## Python implementation (2026-11-01)

The Python backend (`backend/`) and the new frontend (`web/`, scaffolded from the Vite React-TypeScript template) were written independently for Knoventra; no source code from the imported Ragent tree was copied or ported into them. They follow design ideas from Ragent (layering, citation and retrieval flow), which is acknowledged here as a reference. The imported Java sources, `frontend/` and upstream notices are unchanged. New runtime dependencies (FastAPI, SQLAlchemy, LangGraph, pgvector, PyMuPDF, OpenCC, FlagEmbedding / BGE-M3 weights, React, Vite, etc.) remain under their own licenses; the BGE-M3 model weights are downloaded from Hugging Face at runtime and are not redistributed in this repository.
