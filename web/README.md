# Knoventra web

Vite + React + TypeScript, four pages: login (demo account switcher), chat (streaming answer, source cards,
source preview, confirm card, refusal state, conversation replay), owned escalation history, and admin
(upload, revisions, publish).

```bash
cd web && npm install && npm run dev      # proxies /api to KNV_API (default http://localhost:8000)
```

UI-only check without an LLM key: `backend/dev/mock_server.py` (canned answers; shows flow, not answer quality).

## File parse rejection

Uploads rejected with 422 / `DOCUMENT_PARSE_INVALID` show a specific instruction to use
UTF-8 Markdown or valid, unlocked PDF text without NUL. The form stays editable with its
fields and file choice intact for explicit correction; the client does not retry or report
an uncertain commit for this recognized rejection. Other error handling remains unchanged.

## Preparing an existing document's next revision

Each catalog row has “填入新修订”. It copies the exact KB, title (including original
spacing/script) and language into the upload form, replaces any prior metadata, clears
the previous file choice and focuses file selection. The button describes this replacement
in its tooltip; the resulting message asks for a new file and explicitly says it will be
uploaded as a draft. It makes no request and does not reserve a version or publish anything.

The form remains editable; changing KB/title can create a separate document under the
existing server identity rules. Choose a file and explicitly upload, then publish separately.
This shortcut requires a verified catalog and supported KB/language and is disabled during
any active operation. Backend validation and version allocation remain authoritative.

## Admin catalog filters

The document table supports a literal, case-insensitive title substring, an exact KB
selection, and a revision-state filter. Conditions combine with AND. State filters match
documents containing at least one such revision; all revisions of a matching document
remain visible for version comparison. Revisionless documents appear with no state filter.
The table shows matching/total document counts, an empty-match message and a clear action.

Filters apply only to the already loaded catalog and issue no requests or mutations.
They survive explicit refreshes and successful writes followed by refresh; clearing them
does not reset the upload form. A selected KB absent from refreshed data stays explicitly
selected until cleared. Failed refreshes keep the existing unverified-data warning and
write lock even when filtering. Leaving the page resets filters. Title search does not
search body text, normalize scripts or interpret wildcard characters. This does not add
server pagination or reduce the full-catalog response size.

## Identity loading

Each protected route entry verifies `/me` before mounting chat, records or admin. Token or route changes
discard the old identity/page boundary and cancel its request; returning to a previous route still
requires a fresh check. Late responses cannot replace the new account. An identity-load failure offers
explicit retry or account switching, without repeatedly requesting in the background. Login itself
does not load protected identity. Same-page token updates and other-tab localStorage changes invalidate
the boundary; listener subscriptions are removed on unmount. An older request's 401 does not erase a
newer token, and an already canceled request cannot force logout. Current-session 401 still clears the
token and returns to login. These UI checks do not replace backend authorization or cancel committed
work: changing routes/accounts can abort the browser connection while server processing continues.

## Login submission

Choosing an account starts one cancellable login request and disables every account button until it
fails or leaves the login screen. Repeated clicks do not create parallel requests. Failures offer an
explicit retry, without background resubmission; leaving cancels the request. The client checks the
response token before storing it, ignores a canceled response, and rejects a result if credentials
changed while it was pending. Navigation only uses the still-current committed result. Success uses
hash routing and the existing identity check, without a full-page reload. Cancellation does not revoke
a token already issued by the server; this is a client submission guard, not cross-tab transactional
login serialization or JWT validation. Server `/me` remains the identity authority.

## Conversation list

The sidebar uses the owned cursor API, loading 20 conversations initially. “加载更多会话” appends the next
page and deduplicates IDs; “刷新会话” restarts from the newest page without changing the currently open
conversation or its messages. A pagination or same-filter refresh error preserves existing rows and the current cursor for an explicit
retry. List reads have separate loading/error state and never start a model or resume a checkpoint.
New completed requests refresh the first page and cancel an older in-flight list read; late results
cannot overwrite that refresh. Leaving chat cancels the current list request. Upgrade the backend
alongside this frontend; the old array endpoint is retained for other clients.

Submit “会话标题关键词” with “搜索会话” to filter your titles. Typing alone does not query the server;
the active submitted filter is displayed separately and retained during paging, refresh and post-run
refresh. Changing the filter clears old results/cursor and cancels the old request so different result
sets cannot mix. Clear the field and submit to return to all titles. An unsuccessful new filter shows
an error with no old results; explicit refresh retries that submitted filter. Search does not change the
active conversation/messages, open checkpoints or call a model. It matches title text only, not the full
conversation; text beyond the initial title prefix cannot be found by this feature.

## Question input

Chat displays the authenticated server's `chat_max_chars` limit (default/fallback 4,000). It counts
Unicode code points in the trimmed text actually sent, so ordinary emoji do not count twice as UTF-16
surrogate pairs. Oversized text stays editable and is not sent or truncated. Whitespace-only text is
not sent. Server validation failures show an error and restore the rejected question when the composer
has no newer draft; no automatic replay is performed. This is a per-question limit, not a history,
token-count or billing cap. The server rejects invalid questions before conversation processing;
existing saved messages and confirmation/recovery actions are unaffected.

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
Incomplete checkpoints without an interrupt expose `retry_checkpoint_id` and an explicit continuation
button. It posts to `/chat/retry` with that checkpoint ID, preserving the original question and stored
decisions. State reads never execute a retry; stale/finished/pending-confirmation runs are rejected and
the page reloads current state. Recovery can repeat model calls and is always user initiated.
Completed, tracked checkpoints whose final message is missing expose `recover_checkpoint_id` and
a separate save-only button. It posts to `/chat/recover`, which preserves the original answer, source
cards and refusal state without invoking a model or tool. Save failures remain visible after state
refresh and leave the repair action available. Legacy turns without tracking metadata are not inferred.

```bash
npm test        # API streaming and React recovery interaction regressions (jsdom)
npm run build   # TypeScript and production bundle
npm run lint
```

These checks use mock API responses. They verify UI behaviour and do not measure real model answers.
For the complete demonstration sequence, see [the demo guide](../docs/demo-guide.md).

The SSE client requires a complete `done` or `confirm` event before treating a stream as finished.
EOF/read failure before that point raises `STREAM_INCOMPLETE`; malformed UTF-8/JSON or invalid event
payloads raise `STREAM_INVALID`. LF, CRLF and CR delimiters, fragmented UTF-8, heartbeat comments and
multiline JSON data are supported. The reader is canceled and unlocked after a terminal event, parser
failure or early consumer exit; an open connection after `done` cannot keep the UI waiting.
The chat displays the failure and reloads the known conversation state, restoring a saved answer,
confirmation, retry or save-only recovery action as appropriate. It never automatically resubmits a
request. Until state synchronization succeeds the composer stays blocked. If a new conversation's
metadata was never received, its server ID is unknown; refresh the list and reopen it to inspect results.
That interrupted composer is blocked as well; the client does not invent an ID or claim to have synced it.

## Source preview

Source cards are keyboard-operable buttons. Opening one shows loading immediately and fetches the
source with a fresh authorization check; the response must match the requested chunk ID. Closing via
the button, Escape or backdrop cancels the request. Switching context or leaving chat also cancels it;
late responses and failures cannot reopen an old dialog or overwrite a newer source. No full-text
preview cache is used. Failure shows an unavailable/unauthorized/missing-source message without old
content; reopen the card to retry. The modal traps keyboard focus on its close control, makes the
background inert, and returns focus to the source button after closing when that button still exists.
Component tests cover ordering and cancellation; they do not replace real-browser accessibility checks.

## Document management

The admin page shows list loading/failure/empty states and an explicit “刷新资料” action. Upload,
publish and list refresh share a single active-operation guard; write controls remain disabled until
the current list has loaded. Confirmed mutations refresh that list before enabling another write.
Success and refresh failure are separate: a failed follow-up read does not turn a successful publish
into a reported rollback. If a mutation response is lost, the outcome is unconfirmed; the page blocks
another write until an explicit list refresh and never automatically resubmits. Failed uploads retain
their form fields; confirmed uploads clear the form and remain drafts until published separately.
Leaving the page cancels its browser request and ignores late responses. Cancellation does not undo
a committed upload/publish, and a fresh list is only a snapshot; inspect revisions before retrying an
uncertain action. These client guards do not replace server authorization, document locks or constraints.

When the catalog advertises `upload_max_bytes`, the page displays the byte limit and rejects an oversized
selected file before sending it. The server remains authoritative (older catalogs without this field
still work). HTTP 413 is shown as a rejected upload, preserves the form and permits selecting a smaller
file without repeating the failed request or demanding a list refresh. Other uncertain submission
failures retain the existing explicit-verification flow.

The catalog's optional `upload_rules` supplies the title character limit and language choices; older
responses fall back to 255 and `zh-Hans` / `zh-Hant` / `en`. Titles are checked for non-whitespace text,
NUL and code-point length before sending, without rewriting/truncating accepted text. A server 422 is
shown as a rejected upload: the form stays intact and editable for explicit correction, without a forced
catalog refresh or automatic resubmission. Network/uncertain failures still require catalog verification.
These checks validate metadata, not the file's actual language; the server rechecks existing-document
language and retains its lock-based final check.

A recognized 503 / `ENCODER_OUTPUT_INVALID` identifies invalid encoder results before ingestion. The
page preserves the form and explains the service fault without automatic replay. Unknown service/network
errors keep the conservative verification flow. API errors expose a structured uppercase machine code
when supplied as `detail` or `detail.code`, retaining their original status and message otherwise.

## Escalation records

Open “转交记录” (`#/escalations`) to view your confirmed transfers without asking the model. Records show
summary, current status and creation time; legacy rows display “历史时间未知”. Refresh starts at the newest
page; “加载更多” appends older records using the owned cursor. A failed page keeps existing records and
the cursor so the user can retry. Leaving the page aborts the active fetch, including pagination requests.
The page is read-only; canceling a confirmation creates no list record.

CI reproduces `npm ci`, component/API tests, production build and lint on Linux / Node 24 from the
checked-in lockfile. Current frontend lint passes without warnings; see [the CI guide](../docs/ci.md).
