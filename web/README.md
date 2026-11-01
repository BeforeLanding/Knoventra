# Knoventra web

Vite + React + TypeScript, three pages: login (demo account switcher), chat (streaming answer, source cards,
source preview, confirm card, refusal state, conversation replay) and admin (upload, revisions, publish).

```bash
cd web && npm install && npm run dev      # proxies /api to KNV_API (default http://localhost:8000)
```

UI-only check without an LLM key: `backend/dev/mock_server.py` (canned answers; shows flow, not answer quality).
