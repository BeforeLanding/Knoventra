# Knoventra backend (MVP)

Implemented: permission-aware hybrid retrieval, transactional revision publishing, LangGraph agent, citation validation, SSE chat, confirmed/idempotent escalation, evidence preview, and exact fault-code / part-number checks. The React frontend lives in `../web`. Real BGE-M3, LLM and a paired local `bge-reranker-base` retrieval measurement are recorded in [the evaluation report](../docs/evaluation-report.md); automated tests use an offline hash encoder and a scripted model. The planned `bge-reranker-v2-m3` has not been measured; Rerank stays off by default.

```bash
docker compose -f deploy/compose.yaml up -d  # run from repository root
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

`--split val4 --confirm-final` runs the new 28-case exact-identifier regression set. The stored
`eval/results/val4-retrieval-exact-v1.json` is retrieval-only: it makes no LLM calls. It includes corpus
counts, a digest of the selected cases, identifier policy version and per-case rejection reasons.
Older result files remain historical measurements under their original policies; the new Recall@5 is
measured after identifier matching and before the score gate, so it is not a direct before/after comparison.

## Optional reranking

Configure `KNV_RERANK=bge`, `KNV_RERANK_MODEL` (default `BAAI/bge-reranker-v2-m3`), and
`KNV_RERANK_THRESHOLD` (default `0.60`). The latter is independent of `KNV_GATE_THRESHOLD` and is not
calibrated on the corpus. Restart the API after changing model configuration. The `ml` extra and locally
available model weights are required for offline execution; keep `KNV_RERANK=off` for the normal default.

The reranker scores `section_path + text`, so a fault code inherited from a heading remains available.
Queries use the same identifier-format normalization as recall. Scores must be finite numbers in `[0, 1]`,
with exactly one score per candidate; invalid output fails the retrieval rather than falling back to dense
similarity. Final candidates still undergo exact-identifier and score gates. Source text is preserved.

For the paired val5 comparison, use the same frozen 28 cases with each configuration once:

```bash
KNV_ENCODER=bge-m3 KNV_RERANK=off HF_HUB_OFFLINE=1 uv run python eval/run_eval.py --split val5 --confirm-final --out eval/results/val5-reproduction-off.json
KNV_ENCODER=bge-m3 KNV_RERANK=off HF_HUB_OFFLINE=1 uv run python eval/run_eval.py --split val5 --confirm-final --rerank --rerank-model BAAI/bge-reranker-base --out eval/results/val5-reproduction-base.json
```

The stored `val5-retrieval-rerank-{off,base}.json` results share the same case digest and corpus counts.
Metrics now use the pipeline's actual Top-5 **after** optional reranking and before the score gate;
per-case diagnostics preserve that ordering and scores even on refusal, while the agent receives no
refused evidence. Val5's positive examples also require a literal `expect.support_text` in the target chunk,
so another section on the same document/page is not counted as a hit. This checks retrieval relevance,
not whether an LLM answer is supported. Old case files remain unchanged.

`--rerank-model` requires `--rerank`. `--threshold` overrides the active score gate in retrieval mode.
The runner rejects `--no-sparse`, `--no-opencc` and `--threshold` in agent mode because those per-run
ablation options are not implemented by the agent graph; it no longer silently reports unused settings.

## Answer support review

New agent evaluation reports capture a checksummed `review_snapshot` for each case: original question,
final answer, full evidence registry, actually displayed sources, revision/page/section/text and error code.
Standard agent reports also record the system-prompt hash; each case records its thread ID. Refusals keep
an explicit empty displayed-source list while preserving retrieved evidence for over-refusal diagnosis.

Use the offline CLI to create a review packet, edit annotations, then validate and summarize against the
original report. It never calls a model or database, and refuses to overwrite existing files:

```bash
uv run python eval/review_answers.py prepare eval/results/new-agent-run.json --out eval/results/new-agent-review.json
uv run python eval/review_answers.py summarize eval/results/new-agent-review.json --report eval/results/new-agent-run.json --out eval/results/new-agent-review-summary.json
```

Supported annotations require named reviewers, a rationale, and exact quotations from captured sources.
Human and agent judgements remain separate; unreviewed units are visible and have no support-rate score.
Legacy reports without source snapshots are rejected rather than reconstructed from today's database.
See [the review guide](eval/REVIEW.md) for verdicts, sampling, attribution, denominators and limits.
The workflow is tested with scripted models; it does not constitute a human audit of real LLM answers.

The agent now recognises both Simplified `【无依据】` and Traditional `【無依據】` leading refusal markers,
strips the marker and citations, and emits `INSUFFICIENT_EVIDENCE` without final source cards in either case.

## Conversation replay migrations

Run `uv run alembic upgrade head` on existing instances. Migration `0002` persists message error codes;
`0003` uses PostgreSQL insertion time (`clock_timestamp()`) for messages instead of transaction-start
time, avoiding random UUID ordering for messages inserted in the same transaction. Existing timestamps
and missing historical error codes are not reconstructed. The web client restores owned checkpoint
state, pending confirmation expiry and persisted refusal states; see [the demo guide](../docs/demo-guide.md).

## Refusal diagnostics

New agent reports preserve per-search status/reason, returned chunk IDs, target expectations and a
separate model-declared-refusal flag. Run offline triage before reviewing suspected over-refusals:

```bash
uv run python eval/analyze_refusals.py eval/results/new-agent-run.json --out eval/results/new-agent-triage.json
```

The tool separates retrieval gate failures, model refusals after retrieval, and operational errors.
It identifies cases whose captured evidence contains the target source span as review candidates,
not proven over-refusals. Legacy reports without captured diagnostics are rejected; output files are
never overwritten. No LLM or database is used. See [the review guide](eval/REVIEW.md) for denominators,
classification rules, unmarked prose refusals and the distinction between a lexical hit and support.

## Concurrent ingestion and publication

Migration `0004` adds unique document identity `(kb_id, title)` and a partial unique index allowing
at most one `PUBLISHED` revision per document. Run `uv run alembic upgrade head` before running the new
ingestion service. Conflicting legacy duplicates cause migration failure; the migration does not
merge documents, retire revisions or discard data automatically.

Ingestion parses and encodes before locking, rejects documents with no extractable text and encoder
result counts that do not match the parsed chunks. Concurrent uploads to the same title/KB share one
document and allocate revision numbers under a document row lock. A revision must retain the document's
language; upload validation failures return HTTP 422. Creating drafts remains synchronous.

Publication locks that same document, refreshes revision status after any wait, and rejects retired
or empty drafts before retiring the current revision. Repeating publication of the active revision
is a no-op that preserves its timestamp. Publishing an older retired revision is rejected even if
the caller cached it before waiting. Different valid drafts are published in lock-acquisition order;
publication does not enforce ascending version numbers. All writes remain in the caller's transaction,
so failures preserve the previous committed revision.

`tests/test_revision_integrity.py` uses independently committed sessions and observes real PostgreSQL
lock waits to verify concurrent publication, stale-state rejection, simultaneous first/repeated uploads,
database constraints, empty/truncated input and migration roundtrips/conflicts.

## Conversation execution locks

`POST /chat` and `POST /chat/confirm` acquire the same nonblocking PostgreSQL advisory lock for an
owned conversation before reading runnable checkpoint state or saving a confirmation decision.
Concurrent mutation attempts return HTTP 409 with `detail: CONVERSATION_BUSY`, before SSE or message
writes. Ownership checks still return 404 for other users. Different conversations can execute independently.

The lease holds a dedicated SQLAlchemy connection for the whole streaming response, across business
transaction commits. Response cleanup closes the iterator before unlocking on completion, send failure
or cancellation, including disconnects before iteration starts. Lock acquisition/release failures discard
the connection rather than returning a possibly locked DB session to the pool. PostgreSQL releases session
locks when the DB session ends; a browser disconnect does not guarantee immediate cancellation of a
model call already executing in a worker thread.

This requires direct PostgreSQL connections or session pooling; transaction-pooling proxies cannot
preserve the session advisory-lock contract. Each active SSE response consumes one extra DB connection,
so size the connection pool for the intended concurrency. No schema migration is introduced this round.
The lock covers these web mutation endpoints; direct graph invocations by the offline evaluator are
outside it and use separate thread IDs. Read-only history/state requests remain unlocked snapshots.

The frontend distinguishes busy conversations from unresolved confirmations and retains the conflict
notice after reloading server state. Reopen the conversation once the other request finishes before
retrying. Tests cover independent PG pools, independent concurrent HTTP requests using the real graph,
duplicate confirmations, ownership, iterator cleanup, cancellation and discarded connection failures.

## Exact identifiers

`E17-42`, `SEAL-9X` and similar explicit hyphenated identifiers containing a digit are canonicalised
(case, full-width characters, common Unicode dashes) for query encoding and matching. Original source
text is preserved. After permission-filtered recall and RRF, candidates must contain at least one
requested identifier in their text or inherited section path; a document title alone is insufficient.
Filtering happens before Top-K and optional reranking. The final evidence must collectively cover
**every** requested identifier, each with a score above the existing gate. Failure returns
`INSUFFICIENT_EVIDENCE` with internal reason `IDENTIFIER_MISMATCH` or `LOW_SCORE` and no evidence.

The agent passes the latest user's original message alongside each model-generated search query;
their identifier sets are combined, including on retries. `/debug/search` exposes `identifiers.required`,
`identifiers.missing`, and candidate stages `fused`, `identifier_match` (when applicable), `selected`.
It remains a permission-filtered candidate diagnostic, not a statement that the score gate passed.

Limits: no recognition of split/implicit codes such as “E17 设备代码 42”, and no guarantee that a source
mentioning a code supports the requested claim. Multi-code questions are conservative: each individual
search must cover all codes from the current user message, so separately searching one code at a time
can be refused. No extra exact-match SQL recall is added; matches outside the existing recall budget
can still be missed.
