# Performance Implementation Log

This file is the running record of Curcle's performance remediation program.
It did not make it into the repository during Phases 0–2 even though the
code comments and commit messages of that era already referenced it
(`git log --oneline` shows the three merge commits below) — this is the
first time it's actually being written down. The Phase 0–2 section is
reconstructed from the real commit history (`git show <sha>`), not from
memory; only facts verifiable in those diffs/messages are included. Phase 3
is documented live, from the session that implemented it.

## Phase 0 — Baseline instrumentation (`2c60ba9`)

Request-timing middleware (method, route template, status, duration,
correlation id), SQLAlchemy query/connection instrumentation (slow-query and
slow-connection-hold logging via safe, parameterized-statement fingerprints —
never literal values), and a read-only Postgres health snapshot script. No
business logic changed. Files: `app/core/perf.py`, `app/db/database.py`,
`app/main.py`, `scripts/perf_db_snapshot.py`.

## Phase 1 — Critical polling/blocking-handler/BGV-query fixes (`07904a0`)

- Six `async def` upload/apply handlers (doc_requests, documents,
  joining_confirmations, public apply, exit_handover, test_public) ran sync
  S3/DB/OCR calls directly on the event loop — with only 2 uvicorn workers,
  one upload could freeze a whole worker for its full duration. Converted to
  plain `def` (thread-pooled by Starlette) with sync file reads. Verified
  live: 5 concurrent GETs during a real upload completed in 255–269ms each,
  matching the solo baseline, instead of stalling behind the upload.
- BGV lookups (`_pick_doc_request`, `_document_for`, `_portal_field` in
  `bgv_ongrid.py`, plus the candidate-portal-by-interviewId lookup in
  `portal_public.py`) pulled the entire `doc_requests`/`candidate_portals`
  table and filtered in Python. Swapped to the existing indexed
  `repo.find()` (JSONB GIN containment).

## Phase 2 — Server-side pagination, dashboard aggregates, pool tuning (`a2afc6f`)

- `GET /api/{resource}?page=N` opt-in `{data, pagination}` contract:
  database-side LIMIT/OFFSET, ILIKE search, validated-allowlist sort, stable
  `id` tie-breaker. No `page` param → unchanged bare-array behavior
  (circle-be and circle-fe deploy independently).
- `GET /api/stats/overview` and `/api/stats/upcoming-interviews`: dashboard
  aggregates via COUNT/conditional-aggregate SQL and one JOIN, replacing 7
  full-table fetches (2 of which the dashboard never even read).
- BGV connection-hold fix: `ongrid_status` no longer holds a pooled DB
  connection across up to 2 sequential OnGrid HTTP calls.
- Calendar cleanup N+1 fix: batched `get_many`/`delete_many`; added an
  explicit 15s timeout to the Google Calendar client (previously none).
- DB pool re-tuned for the real shared/self-hosted deployment (`pool_size`
  12→6, `max_overflow` 8→4, `pool_timeout` 30→10, `pool_pre_ping`
  re-enabled) and a 5s `statement_timeout` applied via a post-connect `SET`
  — not the libpq startup-parameter form, which Neon's pooled dev endpoint
  rejects outright (caught by testing against the real database).

### Post-Phase-2 hotfix — dashboard still slow after the shell fix

Two rounds of live production investigation (first via the user's
authenticated Claude-in-Chrome session, then via a Playwright-controlled
browser the user logged into directly — the assistant never enters
credentials into a production login form):

1. `DashboardShell.tsx`'s bootstrap gate fetched the full `candidates` and
   `employees` tables purely to read `.isLoading`/`.isError`.
2. After fixing that and the user reporting it was still slow: live
   network/timing inspection of the authenticated production session
   (`performance.getEntriesByType('resource')`) found `CommandPalette.tsx`
   and `Breadcrumbs.tsx` — both rendered on every page via `DashboardShell`
   — independently fetching the full, unpaginated candidates/jobs/employees
   tables (production: **1,514 active candidates**, each with full
   extracted resume text). Fixed both; shipped as a dedicated hotfix
   (`625f6af..2085435`).

## Phase 3 — Background jobs, selective caching, resilience, cursor pagination

No Redis, no new containers on the shared Hetzner VPS — two explicit user
decisions made this scope possible without new infrastructure: (1) a
Postgres-backed job queue instead of Redis, reusing the
`identity_outbox_repository.py` / `FOR UPDATE SKIP LOCKED` pattern already
proven in this codebase; (2) dropping the brief's assumed `org_id`/
multi-tenancy cache-key scheme entirely, since every table in Curcle is
genuinely global/single-tenant — there is no multi-tenancy to scope against.

### C1/C2 — Background job queue + OnGrid status sync

- **`app/repositories/job_repository.py`** (new): `background_jobs` table.
  `next_attempt_at` serves as initial schedule, retry backoff, AND
  crash-recovery visibility-timeout lease in one column — no separate
  "stuck job" sweeper needed. `claim_due()` atomically claims via
  `FOR UPDATE SKIP LOCKED`, safe across both uvicorn worker processes.
  Idempotency keys dedupe a submission **only while the existing job is
  non-terminal** (a partial unique index excludes `completed`/`failed`/
  `cancelled`) — this is what lets a *recurring* key like
  `ongrid-sync:{candidateId}` dedupe pending work on every scheduler tick
  without permanently blocking every future occurrence after the first one
  ever completes. (Caught and fixed during implementation, before it ever
  ran in production — see the "non-terminal only" comment on `INDEX_DDL`
  and in `enqueue()`.)
- **`app/services/job_worker.py`** (new): in-process `asyncio` loop
  (`IdentitySyncWorker`'s exact shape), `ThreadPoolExecutor` per-job timeout
  (`future.result(timeout=...)`), exponential backoff with jitter
  (`min(300, 5 * 2**attempts) + up to 50% jitter`).
- **`app/services/job_handlers.py`** (new): `calendar_cleanup_retry` and
  `ongrid_status_sync` handlers, each a factory closing over `database`/
  `settings` bound once at registration time in `main.py`'s lifespan.
- **`app/services/ongrid_scheduler.py`** (new): periodic loop (every 300s)
  that finds BGV records with a missing/stale (>900s) `ongridStatusAt` and
  enqueues an `ongrid_status_sync` job per candidate — the actual OnGrid
  call happens through the job queue (retry/backoff/timeout), not inline in
  the scheduler.
- **`app/api/routes/background_jobs.py`** (new): `GET/POST
  /api/background-jobs/{id}`, `/retry`, `/cancel` — deliberately **not**
  `/api/jobs`, which already means job *postings* (`app/domain/registry.py`).
- **`app/api/routes/calendar.py`**: `cleanup_calendar_events()` now takes an
  optional `job_repo` and enqueues a `calendar_cleanup_retry` job (max 5
  attempts, idempotency key per app-event+google-event id) when the inline
  Google delete fails, instead of just logging it — "record cleanup jobs
  for retry, preserve cleanup auditability." Wired through
  `app/api/routes/candidate_delete.py`.
- **`app/api/routes/bgv_ongrid.py`**: `ongrid_status()`'s body extracted
  into standalone `sync_ongrid_status()`, reusable from both the HTTP route
  and the background job. `StatusResult` gained `stale: bool` and
  `syncedAt: str | None` — on an `OnGridError` from the live call, it now
  returns the last successfully-cached status (never just fails outright)
  and never overwrites that cache with the failure.
- Verified live against the real dev database (not just unit tests): app
  boots, logs `Job worker started for types: ['calendar_cleanup_retry',
  'ongrid_status_sync']` and `OnGrid sync scheduler started`, the scheduler's
  due-candidate query runs successfully, and `GET /api/background-jobs/{id}`
  returns 401 (auth-gated, correctly registered) rather than 404.
- Tests: `tests/test_ongrid_status_fallback.py` (2 tests — fallback and
  clean-failure paths, asserting the cache is never clobbered by a failed
  live call), `tests/test_job_repository.py` (13 tests, DB-gated —
  enqueue/idempotency/claim/lease-expiry/retry/cancel transitions).

### C4 — External service resilience

- **`app/core/resilience.py`** (new): `CircuitBreaker` (closed → open →
  half-open, process-global per uvicorn worker — no shared store, so this is
  "stop hammering a downed service from *this* process," not a
  cluster-synchronized breaker) and `retry_with_backoff()` (bounded,
  jittered — callers decide what's idempotent; it has no opinion).
- `app/services/ongrid.py`: every call wrapped by a module-level circuit
  breaker (`CircuitOpenError` is translated to `OnGridError` at the
  boundary, so no caller needs new exception-handling). `retry_with_backoff`
  applied **only** to the two read-only, genuinely idempotent calls
  (`verification_status`, `consolidated_report`) — `create_individual` and
  `upload_document` are deliberately **not** retried automatically, per the
  brief's own instruction never to retry a non-idempotent external
  operation without a reconciliation strategy.
- `app/services/google_calendar.py`: circuit breaker wrapped around
  `_credentials()`'s token refresh — the one chokepoint every Calendar
  operation (push, delete, OAuth-connect lookup) goes through first, so one
  wrapped call protects all of them.
- Timeouts: both services already had an explicit timeout before this phase
  (OnGrid: 30s via `urllib`'s single connect+read bound — stdlib `urllib`
  has no split connect/read timeout, and splitting it further wasn't judged
  worth a new HTTP library dependency; Google Calendar: 15s, from Phase 2).
- Tests: `tests/test_resilience.py` (9 tests — closed/open/half-open
  transitions with a controllable fake clock, retry budget exhaustion,
  exception-type filtering).

### C — Dashboard cache

- **`app/core/cache.py`** (new): `TTLCache`, process-local, fail-open (any
  internal cache error falls through to a normal recompute — a cache bug
  must never cause an outage). **Explicitly documented limitation**: with
  2 uvicorn workers and no shared store, `invalidate()` only clears the
  copy in the worker that handled the write — the TTL, not invalidation, is
  the real cross-worker staleness bound.
- `GET /api/stats/overview` cached for 15s (`app/api/routes/stats.py`).
  Invalidated from the generic resource router's `create`/`replace`/
  `patch`/`remove` (`app/api/routes/resources.py`) for the
  `jobs`/`candidates`/`employees` resources (the only ones `overview()`
  reads), and from `candidate_delete.py`'s dedicated cascade-delete route.
- Tests: `tests/test_cache.py` (4 tests).

### Cursor (keyset) pagination

- **`app/repositories/document_repository.py`**: new
  `list_cursor_paginated()` — keyset on `(created_at, id)`, opaque
  base64 cursor, one extra row fetched to detect a next page without a
  separate `COUNT(*)`. An **additional**, opt-in mode alongside the
  existing OFFSET pagination from Phase 2 — not a replacement; a bare
  `GET /api/{resource}` with no `page`/`cursor` param is still
  byte-for-byte unchanged. No arbitrary sort field (unlike the OFFSET
  mode) — keyset pagination needs one totally-ordered key, and
  `created_at` already is one once `id` breaks ties.
- `GET /api/{resource}?cursor=<token>` (empty string for the first page) on
  the generic resource router; `ResourceService.list_cursor_paginated` /
  `CursorPage`.
- **Live-verified against the real dev database** before committing: two
  pages fetched back-to-back had zero id overlap, and a deliberately
  malformed cursor raised the expected `ValidationError` (422), not a 500.
- Tests: `tests/test_cursor_pagination.py` (6 tests, DB-gated — including
  the specific case OFFSET pagination gets wrong: a row inserted between
  two page fetches must not shift rows already returned on an earlier page).

### Explicitly scoped out of Phase 3

**OCR-as-a-job** (converting the on-demand `extract_submission_fields`
HR-only endpoint to submit-a-job + frontend poll-for-completion) was **not**
implemented this phase. Rationale, matching the precedent set in Phase 2
(where `CandidateListView` virtualization was similarly scoped out): it is
the one Phase 3 item that requires a coordinated frontend UX change (a
submit → poll loop) rather than a backend-only improvement, and that change
was not validated with the user before being built. The job-queue
infrastructure built this phase (idempotency keys, retry, per-job timeout)
is already shaped to support it directly whenever that frontend work is
scoped — `JobRepository.enqueue()` with an idempotency key derived from
`f"ocr:{request_id}:{doc_type}"` is exactly the shape the brief asked for.

### Deployment

See `docs/PRODUCTION_DEPLOYMENT_CHECKLIST.md`. Per the brief's own
instruction, no new Redis/worker service needed separate VPS-capacity
confirmation this phase, since the user's own decisions eliminated new
infrastructure entirely — noted there for completeness/future reference.
