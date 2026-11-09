# Knoventra backend (MVP)

Implemented: permission-aware hybrid retrieval, transactional revision publishing, LangGraph agent, citation validation, SSE chat, confirmed/idempotent escalation, evidence preview, and exact fault-code / part-number checks. The React frontend lives in `../web`. Real BGE-M3, LLM and local `bge-reranker-base` / `bge-reranker-v2-m3` retrieval measurements are recorded in [the evaluation report](../docs/evaluation-report.md); automated tests use an offline hash encoder and a scripted model. The v2-m3 comparison did not improve target ranks and added two gate refusals at the uncalibrated 0.60 threshold; Rerank stays off by default.

Model responses with `finish_reason: length` end with `MODEL_OUTPUT_TRUNCATED`; responses containing
`invalid_tool_calls` end with `MODEL_TOOL_CALL_INVALID`. Neither executes tool calls from that response,
even if some sibling calls parsed successfully. The final answer is a clear failure message with no
source cards, and the error persists in chat history. Evidence/search traces already obtained remain
available to evaluation diagnostics; `model_finish_reason` records the last model response's reason.
There is no automatic retry. Previously confirmed and committed escalations remain valid if the later
model acknowledgement fails; check the escalation list before making another request. A new question
starts a new turn. Streamed text may appear before the provider reports the finish reason; the final
`done` answer replaces that preview. Detection depends on the adapter supplying the metadata or parsed
invalid-call information; it does not detect unmarked truncation or prove semantic answer completeness.

Parsed JSON tool calls are also validated against the advertised schemas before **any** call in the
response executes. Arguments use strict types and reject extra fields: search requires a nonblank string
of at most 4,000 characters, escalation requires a nonblank summary of at most 2,000 characters, and
status lookup accepts no arguments. Unknown tool names, blank/missing/duplicate call IDs and multiple
escalations in one response fail the entire batch with `MODEL_TOOL_CALL_INVALID`. Server identity cannot
be supplied as a tool argument. Whitespace is checked without rewriting accepted text. A second check
in the tools node also guards retries of older checkpoints. Existing pending confirmation checkpoints
retain their original operation identity; their summaries are not retroactively revalidated.

Retrieval replies now require at least one registered citation. When the most recent tool-call batch
in this user turn contains only `search_knowledge`, an otherwise successful final answer without a valid
`[S1]`-style reference ends with `MODEL_CITATION_MISSING`, a fixed error answer and no source cards.
Previously captured evidence remains in diagnostics. If that retrieval has no evidence and a
`NO_ACCESS`/`INSUFFICIENT_EVIDENCE` code, its final answer is a fixed refusal, even if the model ignores
the refusal instruction. Workflow replies (status/escalation, including mixed batches) and conversation
without a search batch are exempt. Explicit model refusals with retrieved evidence retain the existing
refusal/review flow. This is a citation-presence check, not claim-level support verification; a valid
reference does not prove the answer is correct. Streamed previews may precede the final checked answer.

```bash
docker compose -f deploy/compose.yaml up -d  # run from repository root
cd backend && uv sync
uv run alembic upgrade head
uv run python -m knoventra.db.seed     # KNV_ENCODER=bge-m3 needs `uv sync --extra ml`
uv run uvicorn knoventra.main:app
uv run pytest                          # uses a throwaway `knoventra_test` database and an offline hash encoder
```

Demo accounts and fictional documents: `data/demo/manifest.json` (all demo passwords are fictional).

## Document parse failures

Recognized malformed sources raise `DocumentParseError` and upload returns HTTP 422 with
`detail.code: DOCUMENT_PARSE_INVALID`: unreadable/empty PDFs, password-protected PDFs,
non-UTF-8 Markdown, unsupported direct-ingestion extensions, and NUL in extracted text.
PDF input is opened explicitly as PDF. Valid `.MD` / `.PDF` suffixes remain case-insensitive.
Responses use fixed explanations without exposing local source paths. These rejections
happen before encoding and document/revision writes; the upload route may already have
initialized the encoder factory before parsing. The existing empty-body validation remains.

PDFs are read into a byte stream before native parsing, avoiding retained path handles
on malformed-file opens that can prevent temporary-directory cleanup on Windows. This
adds an in-memory source copy; HTTP uploads retain the existing byte limit, while direct
ingestion has no new size cap. This does not bound decompressed PDF memory, page count or
parsing time, add OCR, or prove all damaged PDFs are detected. Read errors and unclassified
parser exceptions are not wrapped as `DocumentParseError`; existing API error handling
otherwise stays unchanged. Successful extraction still does not establish source quality.

## Admin catalog reads

`GET /api/v1/admin/documents` keeps its existing response shape and administrator-only
access. Catalog assembly uses two reads regardless of document count: ordered KB names
and a single left join of documents with revision summaries. Documents without revisions
retain an empty list, empty KBs remain selectable, and revisions stay in ascending version
order. The projection excludes chunk text/vectors, revision hashes and KB descriptions.
Authentication may issue its own user lookup outside these two catalog reads.

This removes per-document database round trips, but the endpoint still returns the full
catalog and its payload/memory use grows with catalog size. It is not pagination, a frozen
cross-query snapshot or a measured production latency improvement. No schema or client
upgrade is required for this change.

## Model selection and loading

`KNV_ENCODER` accepts exactly `hash` or `bge-m3`; `KNV_RERANK` accepts exactly `off`
or `bge` (case-sensitive). Unknown modes fail settings validation instead of silently
selecting the offline encoder or disabling reranking. Active `bge` reranking requires
a nonblank model name. Dense/sparse dimensions must be positive and must still match
the physical schema and encoder output; these checks do not migrate the database.

Successful encoder loads are cached with their mode/dimensions. Programmatically
changing these settings after loading raises `ModelConfigurationError` rather than
returning an instance from a different configuration. Apply model configuration changes
by restarting the process; existing settings and compiled graphs do not hot reload.
Failed constructors propagate and are not cached or replaced by another model.

First construction is serialized per process for the encoder and reranker, preventing
concurrent cold requests from constructing the same cached model twice. Inference is
not serialized by these locks, and separate worker processes still load separate models.
Explicit `hash` remains available for offline use; `off` does not load a reranker.

## Query encoding batches

Each authorized search makes one checked encoding call for its distinct required texts.
Dense recall uses the identifier-normalized original script. Sparse recall uses its
OpenCC-simplified form when enabled: identical text reuses the same output pair, while
different scripts form a two-item batch in original/simplified order. Dense-only searches
skip OpenCC and the unused second input. Access checks still precede conversion/encoding,
and the entire response batch is validated before any candidate/session SQL.

New evaluation reports record `config.query_encoding_policy: distinct-batch-v1`.
There is no cross-request cache, query-text retention or change to ingestion, stored
vectors, thresholds or source text. Offline vectors match the previous separate-call
policy; real model batching may have numerical differences and has not been measured
in this iteration. Historical measurements do not establish the new policy's quality
or latency. This removes duplicate input work without claiming a measured speedup.

## Query vector numerical policy

New searches serialize query vectors at float32 round-trip precision with pgvector's adapter instead
of rounding dense coordinates to six decimal places. Sparse query weights are likewise canonicalized
to float32 and zero entries removed, without rewriting encoder output objects. A dense query that is
all zero after conversion does not run the undefined cosine channel; a nonempty usable sparse channel
can still recall authorized evidence. If neither channel has signal, no candidate/session SQL is issued.
The permission-scope check still precedes encoding.

NULL or nonfinite candidate distances are excluded from their channel before RRF. Another valid channel can
still contribute the same chunk. `/debug/search` and retrieval reports expose authorized rejected IDs
under `dense_unscorable` / `sparse_unscorable` when present; rejected text is not passed as evidence.
This can reduce the returned candidate count and does not issue extra recall requests to refill it.
New reports mark `config.query_vector_policy: float32-finite-v1`. No thresholds, stored vectors or
historical results are rewritten; past model measurements are not proof of the new policy's quality.

## Encoder output validation

Both ingestion batches and query encoding use `encode_checked` before document insertion/locking or
candidate SQL. It checks result count and dense/sparse pair shape, configured dense dimension, finite
numeric values within float32 magnitude, and sparse integer indices in `[0, sparse_dim)` with finite
nonnegative weights. Boolean/string vector values and boolean/non-integer indices are rejected. Empty
sparse maps and zero dense vectors retain existing encoder compatibility; no normalization or fallback
encoder is applied. Validation checks the response contract, not semantic quality or every DB constraint.

BGE's adapter also checks dense and lexical result counts independently before conversion, so missing
or extra members cannot be silently discarded by `zip`. An empty batch avoids calling the model.
Malformed adapter output raises `EncoderOutputError`. Ingestion validates each entire batch before
creating a document, revision or chunk; upload reports this service fault as 503 / `ENCODER_OUTPUT_INVALID`
instead of blaming document validation. Query faults stop before candidate SQL and surface through the
existing operational-error/recovery path rather than a no-evidence refusal. Existing vectors and reports
are not rewritten; dimension settings still need to match the physical database schema.

## Document metadata preflight

Upload titles must contain non-whitespace text, contain no NUL and fit the existing 255-character
database column. Accepted title text is preserved exactly: there is no trimming, case conversion or
Unicode normalization that could merge document identities. Supported declared languages are
`zh-Hans`, `zh-Hant` and `en`; an existing document's next revision must use its original language.
The upload route checks metadata before copying the file or loading the encoder, and `add_revision`
repeats the shared check before parsing/encoding for non-HTTP callers. A read-only preflight does not
autoflush pending work. The original document lock and language check after encoding remain necessary
for competing first uploads that both initially saw no document.

Metadata failures return 422 with `detail.code: INVALID_DOCUMENT_METADATA`; missing required form
fields still use FastAPI's validation response. `GET /admin/documents` advertises the title
limit and ordered language choices through `upload_rules`. This validates declared metadata, not the document's actual language
or factual content. Existing records are not rewritten, and no database migration is required.

## Chat question limit

`KNV_CHAT_MAX_CHARS` is a positive integer (default 4,000), exposed to authenticated clients through
`GET /me` as `chat_max_chars`. A new `POST /chat` question must be a nonblank string without NUL and
fit the configured number of Unicode code points. Invalid inputs fail request validation with 422
before the route creates a conversation, inserts messages or advances a checkpoint. Valid API input is
preserved, including its whitespace; the count includes all characters actually submitted. Restart the
backend after changing the limit. FastAPI dependency initialization order is unchanged by this check.

The limit applies to a single new user question, not total history, model input/output tokens, retries,
confirmation or save-only recovery; it is not a spending/context budget. Existing history remains intact.
The browser uses the advertised limit, counts its trimmed outgoing question and never silently truncates
it. After a server validation rejection the question is restored for editing unless a newer draft exists;
there is no automatic resubmission. Older identity responses without the field use a 4,000-character UI
fallback, while the backend always remains authoritative.

## Conversation list pages

`GET /api/v1/conversations/page` returns `{items, next_cursor}` for the authenticated user's sessions,
ordered by creation time descending and ID descending. `limit` defaults to 20 and accepts 1–100;
pass the last returned cursor as `before` for the next page. Missing or another user's cursor returns
404 with the same visibility rule. Items contain ID, title and creation time; listing does not load a
checkpoint, messages, encoder or model. Refresh from the first page to see newer sessions. Cursor pages
are a live ordered listing, not a frozen database snapshot; they avoid offset shifts when newer rows
are inserted. The legacy `GET /conversations` array response remains available for compatibility.
The web sidebar uses the new endpoint, so frontend/backend should be updated together. No schema migration.

The page endpoint also accepts `q` for case-insensitive literal title containment, with surrounding
whitespace removed. Blank means unfiltered; the input is limited to 128 characters and rejects NUL.
`%`, `_` and the escape character `/` are literal text, not user-provided SQL wildcards. Scope remains
the current user. A `before` anchor must belong to that user's current filtered set or returns 404;
clients must reset pagination when changing filters. This searches titles only (normally the first
30 characters of the initial question), not message contents or knowledge-base text. No full-text
index, semantic retrieval, glyph normalization or model call is involved.

## Long paragraphs

New Markdown/PDF ingestion enforces the existing 700-character chunk-text maximum even for paragraphs
without sentence punctuation. Sentence boundaries remain preferred. Short ASCII/fullwidth words and
hyphenated codes (including horizontal spaces around the hyphen) crossing a hard boundary move intact
to the next chunk when they fit within 700 characters. An individual token longer than that limit must
be split. Page and section metadata remain attached to each chunk; source files and content hashes are
unchanged. As before, whitespace at chunk edges may be stripped. The limit counts Python characters,
not model tokens, bytes or Unicode grapheme clusters; heading-path length is outside the text limit.

This applies only to newly ingested revisions. Existing chunks/vectors and historical measurements
are not migrated or regenerated. Re-upload creates a new draft for explicit review/publication. A
better size bound is not a measured retrieval improvement. PDF parsing now uses the supported
`pymupdf` import of the already locked dependency, without changing dependency versions.

## Upload size

`KNV_UPLOAD_MAX_BYTES` sets the per-file limit (positive integer; default 20 MiB / 20,971,520 bytes).
The upload handler copies the parsed multipart file in chunks of at most 64 KiB, reading at most one
byte beyond the limit to detect excess. Exactly-at-limit files are accepted; oversized files return
HTTP 413 with `detail.code: UPLOAD_TOO_LARGE` and `detail.max_bytes`. Empty files return 422. Both fail
before loading the encoder, parsing documents or creating a revision, and the temporary copy is removed.
The currently published revision remains unchanged. Restart the backend after changing configuration.

`GET /admin/documents` includes `upload_max_bytes` so the UI can display and precheck the current limit;
the server always checks actual file bytes regardless of client metadata. Multipart parsing/spooling
occurs before this application guard. This is a per-file input limit, not a total HTTP-body,
decompressed-text, page-count or CPU-time limit. Browser cancellation does not undo committed uploads.

## Evaluation
`eval/cases.jsonl` (60 cases, regenerate with `eval/build_cases.py`): 20 dev (tuning) / 40 holdout (final only).
`uv run python eval/run_eval.py --split dev` runs retrieval metrics without an LLM; ablation flags
`--no-sparse --no-opencc --rerank`. Holdout needs `--confirm-final`.

Agent evaluation requires `KNV_LLM_*`, explicit `--allow-llm`, and both input/output prices per
million tokens in the same currency. Inspect the selected cases before enabling calls:

```bash
uv run python eval/run_eval.py --split dev --mode agent --case-id ops-01 --dry-run --out eval/results/agent-plan.json
```

Repeat `--case-id` to select more cases; unknown IDs fail and repeated IDs are deduplicated in dataset
order. Dry-run loads neither database nor models. It reports case IDs/digest, categories, model name,
graph model-call bound, requested output-token bound, and `confirmation_decision_cases` (whose scripted
decisions may create escalation records in a real run). Use a dedicated, seeded evaluation database;
checkpoint state and committed escalation records persist there. Retrieval mode does not call the LLM.

After checking current provider prices and obtaining authorization for paid calls, use the same flags
with your prices. These shell variables must contain your actual prices, not example defaults:

```bash
KNV_ENCODER=bge-m3 uv run python eval/run_eval.py --split dev --mode agent --case-id ops-01 --allow-llm --input-price-per-million "$INPUT_PRICE" --output-price-per-million "$OUTPUT_PRICE" --currency "$PRICE_CURRENCY" --max-output-tokens 1024 --assumed-input-tokens 4000 --out eval/results/new-agent-run.json
```

Add the price/token flags to dry-run to preview the same estimate. The estimate assumes 4,000 input
tokens per call by default; this is **not an input limit or billing cap**. It multiplies the graph's
tool-budget-derived call bound by the assumed input and maximum requested output cost. Provider token
semantics, reasoning/cache charges and failed requests can differ. Each evaluation request sets
`max_tokens` (default 1,024) and disables automatic provider retries; output limiting depends on the
provider honoring that parameter. The web application's model settings are unaffected. An interrupted
run may incur charges without producing a complete report. Dry-run uses `mode: preflight`, so it cannot
be mistaken for an answer-review report. Existing output paths are rejected; use separate plan/report
paths. Final reporting splits still require `--confirm-final`, including in dry-run.

Real Agent runs create an append-only progress log before loading models or opening the evaluation
database. The default path is `<report-path>.progress.jsonl`; override with `--progress-out`. Existing
logs are never overwritten, and report/log paths must differ. Every event is flushed and fsynced:
`run_started`, `corpus_captured`, `case_started` (with checkpoint thread ID), `case_completed` (the full
answer, captured evidence and diagnostics), followed by `run_completed` after the final report is saved.
An exception or Ctrl+C records `run_failed` with its class only, then propagates the original error.
The log contains the same source text as the report; apply the same file access policy.

Completed cases survive later model/database failures. A failed run does not publish aggregate metrics;
the JSONL log is diagnostic evidence, not a complete Agent report or direct answer-review input. There
is no automatic resume/retry: re-running with new paths may call completed cases again. An in-flight
case may already have incurred costs or committed an escalation. A process kill/power loss can leave
no terminal event or a truncated final line; inspect the last complete records and checkpoint IDs rather
than assuming success. Disk/write failure stops the run; a failure while saving the final report may
leave an incomplete report file, which must not be treated as a completed result. Dry-run creates no log.

Inspect logs offline with `uv run python eval/inspect_progress.py <progress.jsonl> --out <inspection.json>`.
It distinguishes completed, in-flight and unstarted cases and validates event order, thread identity and
captured snapshots. `--report <report.json>` additionally checks selection/preflight, corpus, results and
recomputed metrics against a successful run; unlogged config is outside that check. Stored paths are
never followed automatically. Use `--allow-truncated-tail` explicitly to discard an unterminated final
line from a stopped run; corrupt middle lines remain errors. Inspection neither resumes a run nor calls
models/DBs, and its output cannot serve as a complete evaluation or answer-review report. See
[the review guide](eval/REVIEW.md) for status and integrity limits.

`--split val4 --confirm-final` runs the new 28-case exact-identifier regression set. The stored
`eval/results/val4-retrieval-exact-v1.json` is retrieval-only: it makes no LLM calls. It includes corpus
counts, a digest of the selected cases, identifier policy version and per-case rejection reasons.
Older result files remain historical measurements under their original policies; the new Recall@5 is
measured after identifier matching and before the score gate, so it is not a direct before/after comparison.

## Optional reranking

An isolated real-model comparison runner creates/seeds a fresh temporary database, runs val5 once
with reranking off and once with the selected model, checks paired corpus/case fingerprints, and
drops that database in cleanup. It requires cached model weights and never calls an LLM:

```bash
uv run python eval/run_rerank_pair.py --confirm-final --out-dir eval/results/new-v2m3-comparison
```

`KNV_TEST_ADMIN_URL` supplies a test admin connection with CREATE/DROP DATABASE privileges; the app
database is not used. Each subprocess releases GPU memory before the next configuration. Existing
output directories and result files are rejected. New reports record dependency versions,
published-text/grant fingerprints and model names. Adapter commit fields are null when the loaded
wrapper does not expose a revision; observed cache snapshots for Nov 7 are separately recorded in
`eval/results/val5-v2m3-pair-20261107/cache-provenance.json`, not fabricated as adapter metadata.
Text fingerprints exclude random IDs and vectors; grants have a separate digest.

Configure `KNV_RERANK=bge`, `KNV_RERANK_MODEL` (default `BAAI/bge-reranker-v2-m3`), and
`KNV_RERANK_THRESHOLD` (default `0.60`). The latter is independent of `KNV_GATE_THRESHOLD` and is not
established as a production threshold. Restart the API after changing model configuration. The `ml` extra and locally
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

For a separate, score-only threshold check, `eval/cases_threshold_v1.jsonl` freezes 12 calibration and
12 validation cases. On an isolated seeded database with cached local models, run:

```bash
KNV_ENCODER=bge-m3 KNV_RERANK=bge HF_HUB_OFFLINE=1 uv run python eval/calibrate_rerank.py --out eval/results/new-threshold-check.json
```

The script measures Top-5 target presence and normalized reranker scores, selects a threshold using only
the calibration half, and reports validation errors without changing server settings or calling an LLM.
The stored result and its small-sample limits are in [the evaluation report](../docs/evaluation-report.md)
section 18. Score alone did not separate every answerable detail from unsupported specifics; Rerank stays
off by default.

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

## Agent latency measurements

### Explicit provider thinking mode

`KNV_LLM_THINKING` accepts exactly `provider-default` (the default) or `disabled`.
`provider-default` omits any vendor thinking extension; it does not mean thinking is disabled.
`disabled` adds `extra_body={"thinking": {"type": "disabled"}}` to the shared web/evaluation
model constructor. Only choose it for providers accepting that extension, such as DeepSeek;
other compatible endpoints keep `provider-default`. Unsupported values fail configuration validation.

DeepSeek currently enables thinking by default and requires reasoning history on subsequent tool
requests ([official guide](https://api-docs.deepseek.com/guides/thinking_mode/)). This project's installed
generic `ChatOpenAI` adapter does not retain provider-specific `reasoning_content`, so configure
`KNV_LLM_THINKING=disabled` for this DeepSeek tool workflow. Enabling thinking is not offered until a
provider-specific adapter and its history contract are implemented and tested. The switch does not
filter or edit responses and does not automatically retry a rejected request in another mode.

Changing the environment requires a backend process restart; the web graph caches its model instance.
The evaluation process reads the same setting, includes it with streaming mode in the offline preflight
and final report, and the progress inspector checks logged options against the report. Historical
reports without these fields remain valid and their actual thinking mode is not inferred. Web retry
policy remains one provider retry; evaluation still has zero retries and its explicit output cap.

### Wall and retrieval time

New Agent evaluation reports include `diagnostics.retrieval_trace[*].latency_ms` for each completed
search, including searches rejected by access or evidence gates. The monotonic wall clock covers
opening the tool's database session, retrieval (encoding, recall, optional reranking and gating),
and closing that session. Exceptions that abort a search do not produce a completed trace.

Agent `metrics.latency_ms` summarizes the existing per-case wall time, including graph invocation,
scripted confirmation/resume and result assembly. `metrics.retrieval_latency_ms` summarizes individual
completed searches, so a case with multiple searches contributes multiple samples. Both include
`n`, `p50`, `p95`, and `max`; percentiles use nearest rank (`ceil(p*n)`, one-based). Retrieval also
reports `completed_calls`, making missing timings in legacy traces visible. Missing or invalid samples
are excluded, never converted to zero; empty distributions have null percentiles and maximum.

New Agent reports identify this definition as `config.latency_policy=agent-wall-and-completed-search-v1`.
The offline progress inspector verifies these timing aggregates for that policy, preserves the original
metric contract for legacy reports, and rejects unknown timing policies.
Existing report files and retrieval-mode summaries are unchanged. This is server/evaluator wall time,
not browser latency. It does not measure a person's confirmation delay,
and scripted/Hash-encoder regression timings are not real model performance results.

### First streamed answer content

Agent evaluation now configures `ChatOpenAI(streaming=True)` while preserving the output token cap and
zero automatic provider retries. A per-case callback records `first_answer_chunk_ms`, measured from
the beginning of that case to the first nonempty string content chunk of the **last completed model
response without tool calls**. Reasoning-only chunks and tool parameters are excluded. Content emitted
before a tool call is discarded when that response finishes; the answer after retrieval or scripted
confirmation/resume uses the original case start. This includes earlier model calls and retrieval time.

The field is null if the provider/model supplies no streamed content, the response is empty, truncated,
contains invalid calls, or the final graph state reports an operational error (including missing
citations). Ordinary answers and evidence/access refusals can contribute samples. The corresponding
`metrics.first_answer_chunk_ms` reports `n`, `p50`, `p95`, and `max`, using the same nearest-rank convention.

This is an observable approximation of time to first answer token: a provider chunk may contain more
than one token. It is measured at the model callback, before citation/refusal processing, which can
replace the raw response; it does not claim time to first validated answer or browser-visible text.
The callback retains only run IDs and times, never prompt, answer or reasoning text. Each new case gets
its own timer; real human confirmation waiting is not simulated by the evaluator.

Reports declare `config.answer_timing_policy=last-model-content-chunk-v1` and `config.llm_streaming=true`.
The offline inspector validates the added aggregate only for the declared policy, preserves both older
report formats (without timing and with wall/search timing alone), and rejects unknown policies.
New streaming evaluation timings should not be compared directly with old non-streaming runs as a
performance improvement. No historical reports are modified and no real LLM measurement is implied by
the scripted-model tests.

## Final answer script

The graph's citation-check stage now formats final Chinese answer prose using the **current user
question**, with policy `unambiguous-question-opencc-v1`. OpenCC signals must identify one script
unambiguously: Traditional questions receive Traditional final prose, Simplified questions receive
Simplified prose. Shared characters, non-Chinese and mixed/ambiguous inputs leave model output unchanged.
This is script conversion, not language translation or a region-based preference.

Citation aliases, fault codes, inline/fenced code and HTTP URLs remain intact. The evidence registry,
source-card text, section paths and stored documents retain their original spelling; only answer prose
is formatted. Refusal-marker detection and citation validation run first, and refusal/error semantics
are preserved. Each new turn uses its own question rather than inferring a preference from history.
The web stream may show raw model text until `done`; history and final evaluation snapshots contain
the formatted answer. This final formatting is after the first-chunk timing observation.

The 2026-11-09 real dev report predates this formatting change. Its separate script-replay artifact
applies the formatter to captured answers without making another model call, and does not fix semantic
over-refusal or count as a new quality run. The revised completeness instruction likewise has no new
real-model measurement; see evaluation report sections 20–22.

## Direct escalation requests and observed confirmations

The Agent now routes short, clear Chinese requests such as `这个问题我想转人工` through the existing
escalation tool and confirmation workflow before calling a model. The confirmation summary preserves
the user's current request and any earlier user statements, rather than treating prior model prose as
facts. It must fit the tool's existing 2,000-character contract; if the request or combined context is
longer, the model summarization route is retained rather than silently dropping details. A first-turn
request is preserved verbatim except edge whitespace. It does not invent issue details or route to a
specific expert team.

Recognition uses a bounded grammar, not general intent understanding. Negated, conditional, quoted,
informational, ambiguous and unrecognized requests retain the model path. Matching is only performed
when the latest message is a new user message; tool returns and interrupt resumes do not start another
direct operation. The normal tool validation, call limit, owned confirmation, expiry, conversation lock
and operation-id uniqueness rules still apply. No escalation record is written until approval.
New reports record `escalation_routing_policy=unambiguous-chinese-request-v1`.

Evaluation now records `asked_confirmation` from actual checkpoint interrupts. A cancellation case
cannot pass merely because it created zero records if it never showed the expected confirmation.
An unexpected or unresolved confirmation also fails; `record_count_ok` separately preserves the pure
record-count check.
`escalation_metric_policy=observed-interrupt-and-records-v2` identifies the new metric. Older reports
used the nonempty expected-decision list for that boolean and cannot prove a card was observed from
that field alone. Their files are not rewritten; the failed follow-up case has a separate checkpoint
audit. The direct route was implemented after that real run and validated offline, not by an additional
paid rerun.

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

## Confirmation identity across turns

Confirmation requests are matched against the actual checkpoint interrupt **under the conversation lock**
before saving any decision. A previous confirmation cannot resume a later interrupt on the same thread;
a pending row with no matching interrupt cannot be approved. These conflicts return HTTP 409 with
`detail: CONFIRMATION_NOT_ACTIVE`, and the frontend restores the current card and shows a specific notice.
Completed confirmations can still be replayed as no-ops after the thread finishes. An already saved
decision on the active interrupt wins over a different retry request body.

New escalation operation IDs include thread ID, user-message ID and tool-call ID, keeping separate turns
independent even when a model reuses a tool-call ID. The operation ID is stored in the pending tool state
before interruption, so a process restart uses the same ID. Pre-existing checkpoints without that field
retain the legacy thread/tool-call derivation on resume. No migration or historical-row rewriting is needed.
Resuming failed graph nodes without a matching interrupt is outside this confirmation endpoint's contract.

## Recovering incomplete runs

`GET /conversations/{id}/state` now returns `retry_checkpoint_id` for a checkpoint that has pending
nodes and no interrupt. It remains a read: it never runs a model or retries a tool. The web client shows
an explicit continuation button and keeps new-question submission blocked. New questions against an
incomplete non-interrupted run return HTTP 409 / `RUN_INCOMPLETE`, rather than a misleading confirmation error.

`POST /chat/retry` accepts `{conversation_id, checkpoint_id}`. After ownership verification it acquires
the same conversation lease as chat/confirm, checks the current checkpoint ID and rejects completed
runs or interrupts with `RUN_NOT_RETRYABLE`. Stale IDs return `CHECKPOINT_CHANGED`; active requests
return `CONVERSATION_BUSY`. Errors are HTTP 409 before streaming or new message writes. A stale retry
can never execute a later incomplete turn, and pending confirmations must still use `/chat/confirm`.

Recovery streams the graph with `None` input: no new user message, no reset of the existing tool state,
and no new approval decision. Pending nodes may execute again. The only write tool uses the existing
operation ID and database uniqueness constraint, so a committed escalation followed by a node failure
can be retried without creating a second record. A fresh compiled graph over the same checkpointer
can recover after process restart. Existing error messages stay in history, followed by a successful
answer on recovery. A replay after completion is rejected rather than appending a duplicate answer.

Scope: only saved incomplete graph checkpoints are recovered. A completed checkpoint whose final
message failed to reach the history table is not reconstructed here. DB/checkpointer outages still
require service restoration. Model calls may incur costs again when a user explicitly continues a run;
the tests use scripted models only. No automatic retries or schema migration are introduced. New tools
with side effects must provide their own retry/idempotency guarantees before being added to this graph.

## Saving completed checkpoint answers

Migration `0005` adds nullable `knv_conversation.active_turn_id` and `knv_message.completion_turn_id`,
plus unique `(conversation_id, completion_turn_id)` for final messages. Upgrade with
`uv run alembic upgrade head` before starting the new service. It preserves legacy rows without
inventing missing delivery metadata or inferring whether old answers were saved.

New web turns persist a server-generated user-message ID with the user message before starting the
graph. Final answer inserts use that turn ID and an idempotent database insert. If a completed
checkpoint's latest user message matches the tracked turn and there is no final message for it,
state returns `recover_checkpoint_id`, keeps the composer blocked, and offers save-only recovery.
New questions return `ANSWER_NOT_SAVED` until the previous final answer has been saved.

`POST /chat/recover` accepts `{conversation_id, checkpoint_id}`. Under the same owned conversation
lease it checks checkpoint identity, requires completion and an unsaved tracked turn, then inserts
the captured final answer, public source cards and error/refusal code. It never invokes the graph,
model or tools. Stale IDs return HTTP 409 / `CHECKPOINT_CHANGED`; completed delivery, untracked legacy
turns or unfinished graphs return `RUN_NOT_RECOVERABLE`. Other users still receive 404, and concurrent
mutations receive `CONVERSATION_BUSY`. A successful repeated request is rejected without another row.

Final-answer save failures now emit `ANSWER_SAVE_FAILED`, distinct from graph execution failures.
If save-only recovery fails again, the checkpoint remains available for another explicit attempt;
the frontend retains the error notice after state refresh. Earlier error-history rows remain visible.
The final-message uniqueness constraint also protects against repeated delivery across restarts.

Scope: this repairs exact, newly tracked checkpoint output, not missing historical evidence. It does
not read earlier checkpoint versions or regenerate missing answers. Legacy in-flight checkpoints can
still finish normally but are not inferred as candidates for save-only repair without a tracked turn.
The existing `/chat/retry` continues incomplete graphs; `/chat/recover` only persists completed results.

## Owned escalation history

`GET /api/v1/escalations?limit=20&before=<id>` returns `{items, next_cursor}` for the authenticated user
only. Limits are bounded to 1–100. Records are ordered by `created_at DESC NULLS LAST, id DESC`;
the optional cursor must itself belong to the caller (foreign/missing IDs return 404). Keyset pagination
handles equal timestamps and unknown legacy dates, and newly inserted records do not shift older pages.
Refresh starts again at the newest page. The existing single-record endpoint now also returns `created_at`.

Migration `0006` adds the nullable creation timestamp and an ordered user index. It sets a server-side
`clock_timestamp()` default for **new** rows after adding the column, so legacy rows keep unknown dates
rather than receiving a fabricated migration time. Run `uv run alembic upgrade head` before starting
the new code. Idempotent escalation retries retain the original row and timestamp. The model's status
tool also uses time ordering for its latest five records instead of sorting random UUIDs.

The web navigation exposes a read-only “转交记录” page, with current status, summary, creation time,
unknown-history labels, refresh, incremental loading and retryable fetch errors. Leaving the page aborts
the active initial/pagination request. Creation still starts as OPEN; this page provides no status editor.
The API and UI use no LLM calls for listing, and administrators see their own records under the same rule.

## CI and migration verification

The repository workflow runs the locked base/test dependency environment on Linux with a disposable
pgvector service, scripted models and no LLM secrets. `dev/check_migrations.py` creates its own randomly
named test database, checks upgrade/downgrade/metadata parity, repeats the demo seed and drops that
database in cleanup. It uses `KNV_TEST_ADMIN_URL`, not the application database URL. Full pytest uses
the separate `knoventra_test` database. See [the CI guide](../docs/ci.md) for commands and scope.

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

Queries additionally join bounded labelled forms such as `E17 设备报代码 42`, `p03故障码11` and
`S5設備報錯誤碼19` into explicit identifiers before dense/sparse recall and reranking. Models must be
1–8 ASCII letters followed by 1–4 digits; the labelled suffix must contain exactly two digits. Optional
device wording, 报/報, horizontal whitespace and a colon are accepted. Both original user text and model
search text contribute constraints. Source matching still uses explicit codes only and never rewrites
stored source text or derives a code from a document title alone.

Bare numbers, distant references, missing model names, decimal/range/alternative numbers and unit-bearing
forms are not joined. The query policy is recorded as `explicit-and-split-v2` in new evaluation reports.
Historical `exact-v1` reports and case files stay unchanged; no real BGE/LLM evaluation is added this round.

Limits: this is a bounded grammar, not general natural-language code resolution; no guarantee that a source
mentioning a code supports the requested claim. Multi-code questions remain conservative: each individual
search must cover all codes from the current user message, so separately searching one code at a time
can be refused. No extra exact-match SQL recall is added; matches outside the existing recall budget
can still be missed.
