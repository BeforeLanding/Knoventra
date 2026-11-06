# Knoventra web

Vite + React + TypeScript, three pages: login (demo account switcher), chat (streaming answer, source cards,
source preview, confirm card, refusal state, conversation replay) and admin (upload, revisions, publish).

```bash
cd web && npm install && npm run dev      # proxies /api to KNV_API (default http://localhost:8000)
```

UI-only check without an LLM key: `backend/dev/mock_server.py` (canned answers; shows flow, not answer quality).

## Conversation recovery

Select an existing conversation after a refresh to load `/conversations/{id}/state`.
The page restores saved refusal/error states and any pending confirmation with its expiry time.
The composer stays blocked until the confirmation is resolved. Expired confirmations offer a close
action; already saved decisions offer recovery using the server's decision. Failed confirmation
requests reload the checkpoint before allowing a retry, rather than displaying an optimistic approval.
Conversation switching is disabled while a request is active; leaving the chat aborts the browser request.
The server also rejects concurrent chat/confirmation mutations for the same conversation with
`CONVERSATION_BUSY` (HTTP 409). The page retains the busy notice after restoring state; reopen the
conversation after the other request finishes before retrying.

```bash
npm test        # API streaming and React recovery interaction regressions (jsdom)
npm run build   # TypeScript and production bundle
npm run lint
```

These checks use mock API responses. They verify UI behaviour and do not measure real model answers.
For the complete demonstration sequence, see [the demo guide](../docs/demo-guide.md).
