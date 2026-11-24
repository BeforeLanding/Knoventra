# Third-party notices

## Ragent

- Project: Ragent AI
- Repository: https://github.com/nageoffer/ragent
- Imported branch: main
- Imported commit: c3b6157730ac1f66a312bcd286c47d3ee6aaac42
- Import date: 2026-10-26
- License: Apache License, Version 2.0; see [LICENSE](LICENSE).

Knoventra is based on Ragent and retains the imported source history and existing source notices. The original project README was preserved as `README.upstream.md` at that time; the file was removed from the working tree on 2026-11-24 together with the upstream sources (see below) and remains available in the commit history.

Changes in the initial Knoventra documentation commit: replace the root README with a Knoventra introduction, preserve the upstream README separately, and add a secondary-development plan and this attribution file. No application behavior is changed by that documentation commit.

The upstream snapshot contains a root LICENSE and no root NOTICE file. This file records attribution for the import; it does not replace the upstream license or assert ownership of upstream contributions. Dependencies and bundled third-party assets remain subject to their respective licenses.

## Planned Python reimplementation

On 2026-10-27, the development plan was revised to target Python, FastAPI, LangChain and LangGraph while retaining the imported Java baseline for provenance and behavioral reference. The root README and development plan describe planned work, not an implemented Python backend. This documentation update does not change application behavior or remove upstream notices.

On 2026-11-01, the plan was narrowed to a three-week MVP (v3.0): a permission-aware knowledge Q&A service on Python, FastAPI, LangGraph and PostgreSQL / pgvector, with a new lightweight frontend that may port selected components from the upstream `frontend/`. This documentation update likewise does not change application behavior or remove upstream notices.

Any copied or modified upstream code remains subject to its applicable license and attribution requirements. New dependencies and assets will be reviewed under their respective licenses when introduced.

## Python implementation (2026-11-01)

The Python backend (`backend/`) and the new frontend (`web/`, scaffolded from the Vite React-TypeScript template) were written independently for Knoventra; no source code from the imported Ragent tree was copied or ported into them. They follow design ideas from Ragent (layering, citation and retrieval flow), which is acknowledged here as a reference. The imported Java sources, `frontend/` and upstream notices are unchanged. New runtime dependencies (FastAPI, SQLAlchemy, LangGraph, pgvector, PyMuPDF, OpenCC, FlagEmbedding / BGE-M3 weights, React, Vite, etc.) remain under their own licenses; the BGE-M3 model weights are downloaded from Hugging Face at runtime and are not redistributed in this repository.

## Removal of the upstream sources (2026-11-24)

The imported upstream sources are no longer in the working tree: the Java/Maven modules (`agent/`, `bootstrap/`, `framework/`, `infra-ai/`, `mcp-server/`, `rag/`, `system/`), `resources/`, the upstream `frontend/`, `scripts/`, `assets/`, the Maven wrapper and build files, IDE settings, and `README.upstream.md`. The upstream-only documents under `docs/` were removed in the same cleanup: the upstream dashboard statistics note (`docs/dashboard-statistics.md`), the PDF ingestion example and its pipeline request (`docs/examples/`), the upstream release notes (`docs/releases/`), and the two upstream architecture diagrams (`docs/assets/ragent-architecture.svg`, `docs/assets/ragent-architecture-overview.svg`). They remain available in this repository's git history and in the upstream repository at the commit recorded above. The Python backend (`backend/`) and the web frontend (`web/`) were written independently and do not copy upstream code. This attribution and the root [LICENSE](LICENSE) are kept.
