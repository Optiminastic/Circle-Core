# Architecture Overview

High-level map of circle-be. Like the other performance docs, this is
being written for the first time alongside Phase 3 — see
`docs/PERFORMANCE_IMPLEMENTATION_LOG.md` for the detailed phase-by-phase
history behind each subsystem below.

## Stack

FastAPI (sync handlers — `def`, not `async def`, except where genuinely
non-blocking; Starlette thread-pools sync handlers, which is deliberate
after Phase 1's lesson that an `async def` handler running blocking I/O
can freeze an entire uvicorn worker) + SQLAlchemy (sync) + PostgreSQL,
documents stored as a generic `(id, data JSONB)` table per resource
(`app/domain/registry.py` defines the resource catalog). Deployed as 2
uvicorn worker processes on a shared Hetzner VPS (Coolify-managed,
co-located with two unrelated apps — `avora` and `o2` — behind the same
Caddy instance).

## Request path

`route → service → repository → db`, matching this repo's own CLAUDE.md
layering rule. Routes (`app/api/routes/`) parse input and return response
shapes; services (`app/services/`) hold business rules; repositories
(`app/repositories/`) are the only layer that builds SQL, all scoped and
parameterized.

Most resources (candidates, jobs, employees, interviews, bgvs, ...) go
through one **generic CRUD router** (`app/api/routes/resources.py`) backed
by `ResourceService` + `SqlAlchemyDocumentRepository` — one implementation
serves every resource; the registry drives validation. A handful of
resources (candidate delete, calendar, OnGrid, auth) have dedicated routes
where the generic CRUD contract doesn't fit (cascading deletes, OAuth
flows, third-party API shaping).

## Background processing (Phase 3)

Added to run retriable, non-critical-to-the-response work off the request
path without introducing new infrastructure — the existing Postgres
database is the only moving part.

```
  write path                    background_jobs table              worker loop
┌──────────────┐  enqueue()   ┌──────────────────────┐  claim_due()  ┌───────────────┐
│ candidate     │ ───────────▶│ id, job_type, payload,│◀────────────▶│ JobWorker     │
│ delete route, │              │ status, attempts,    │  (SKIP       │ (asyncio task,│
│ sync          │              │ next_attempt_at, ...  │   LOCKED)    │  both uvicorn │
│ scheduler     │              └──────────────────────┘               │  workers)     │
└──────────────┘                                                      └──────┬────────┘
                                                                               │ runs handler
                                                                               ▼
                                                                  app/services/job_handlers.py
                                                                  (calendar_cleanup_retry,
                                                                   ongrid_status_sync)
```

- **`app/repositories/job_repository.py`** — the queue itself. One table,
  claimed via `FOR UPDATE SKIP LOCKED` (the same pattern already proven by
  `identity_outbox_repository.py`), safe across both uvicorn workers. A
  single `next_attempt_at` column is simultaneously the initial schedule,
  the retry backoff, and the crash-recovery visibility-timeout lease.
- **`app/services/job_worker.py`** — the in-process consumer: an
  `asyncio` loop that polls, claims a batch, and runs each job in a
  `ThreadPoolExecutor` with a per-job timeout.
- **`app/services/job_handlers.py`** — the actual job logic, as factories
  bound to `database`/`settings` at registration time in `app/main.py`'s
  lifespan.
- **`app/services/ongrid_scheduler.py`** — a periodic producer: finds BGV
  records whose cached OnGrid status is stale and enqueues a sync job per
  candidate (doesn't call OnGrid itself — that's the job handler's job).
- **`app/api/routes/background_jobs.py`** — `GET/POST
  /api/background-jobs/{id}[/retry|/cancel]`, for operator visibility.

## External service resilience (Phase 3)

`app/core/resilience.py` provides two primitives used by
`app/services/ongrid.py` and `app/services/google_calendar.py`:

- **`CircuitBreaker`** — closed → open → half-open, process-global (one
  instance per service, shared across request-scoped client instances).
  No shared store across the 2 uvicorn workers — each worker's circuit
  state is independent, which is an accepted limitation given no new
  infrastructure was introduced this phase.
- **`retry_with_backoff()`** — exponential backoff with full jitter,
  applied only around calls the caller has confirmed are idempotent
  (reads; never a create/upload without its own idempotency key).

## Caching (Phase 3)

`app/core/cache.py`'s `TTLCache` is a small, process-local, fail-open
cache — currently backing `GET /api/stats/overview` (15s TTL). Explicitly
not a consistency mechanism: with no shared store, invalidation (hooked
into the generic resource router's write paths) only clears the cache in
the uvicorn worker that handled the write; the TTL is the real
cross-worker staleness bound.

## Pagination

Two modes coexist on the generic list endpoint, both opt-in:

- **Offset** (`?page=N`, Phase 2) — `{data, pagination}` with a `total`
  count, database-side LIMIT/OFFSET/search/sort.
- **Cursor/keyset** (`?cursor=<token>`, Phase 3) — no `total`, no
  OFFSET-N row-skipping cost, stable under concurrent inserts. Built for
  `candidates`, the confirmed-large table, but available generically.

No `page`/`cursor` param at all → the original bare-array response,
unchanged, so an older deployed frontend build keeps working against a
newer backend (circle-fe/Vercel and circle-be/Coolify deploy
independently).

## What's intentionally not here

- **No Redis, no message broker, no separate worker container.** Two
  explicit user decisions during Phase 3 scoping: a Postgres-backed queue
  over Redis, and dropping the brief's assumed multi-tenancy scoping
  entirely, since Curcle's actual data model is single-tenant throughout.
- **No OCR-as-a-background-job yet** — scoped out this phase because it
  needs a coordinated frontend change (submit → poll) that wasn't
  validated with the user; the job-queue primitives already support it
  directly whenever that frontend work is scoped.
