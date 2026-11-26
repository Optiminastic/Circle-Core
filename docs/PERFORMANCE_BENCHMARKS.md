# Performance Benchmarks

Every number here was actually measured in the session that made the
change — no estimates, no extrapolation. Each is labeled **local** (this
developer's dev environment against the pooled Neon dev database) or
**production** (the live Hetzner-hosted deployment). Like
`docs/PERFORMANCE_IMPLEMENTATION_LOG.md`, this file didn't make it into the
repo during Phases 0–2 despite being referenced from that era's commits;
the numbers below for those phases are the ones that commit messages
already recorded.

## Phase 1 — blocking upload handlers (production)

5 concurrent `GET` requests fired during a real file upload:

- **Before:** the upload handler ran synchronously on the event loop;
  concurrent requests on the same uvicorn worker stalled behind it for the
  full upload duration.
- **After:** all 5 completed in **255–269ms** each — matching the normal
  solo-request baseline, i.e. no measurable stall from the concurrent
  upload.

## Phase 2 — dashboard aggregates / N+1 fixes

No specific before/after timing numbers were recorded in the Phase 2
commit message beyond the structural change itself (7 full-table fetches →
2 aggregate-SQL endpoints). Documented here as a known gap rather than
backfilled with an invented number.

## Post-Phase-2 hotfix — dashboard load (production)

Qualitative, not timed: before the fix, the dashboard's main content area
was observed stuck on its loading shimmer on a real production load
(user-reported, confirmed via screenshot). Root cause was three separate
components (`DashboardShell`, `CommandPalette`, `Breadcrumbs`) each
independently fetching the full, unpaginated `candidates`/`jobs`/
`employees` tables — production `candidates` alone measured **1,514 active
rows**, each carrying full extracted resume text. No isolated before/after
duration was captured for this specific fix; the fix removed 3 full-table
fetches from every page load.

## Phase 3 (local, dev database — DDL/query round-trip only)

Phase 3 introduces new capabilities (background jobs, circuit breakers,
caching, cursor pagination) rather than fixing a previously-measured
bottleneck, so there is no "before" number to improve on for most of it.
The numbers below are round-trip timings observed against this
developer's pooled Neon **dev** database while verifying the new code
actually runs — they characterize this specific dev connection's latency,
not production's self-hosted, unpooled Postgres, and should not be read as
a production benchmark:

- `background_jobs` table + 3 index creations (one-time, on first boot
  after this phase deploys): **~230–420ms per DDL statement** (4
  statements, ~1.2s total) — in line with every other table/index creation
  measured the same way during this same local run (consistently
  230–320ms per statement against this connection).
- `JobRepository.claim_due()` (one `UPDATE ... RETURNING`, empty queue):
  **~240–300ms** per call against the dev connection.
- `OngridSyncScheduler._find_due_candidate_ids()` (one `SELECT` over
  `bgvs`, dev data volume — a handful of rows, not production's scale):
  **~260ms**.
- `list_cursor_paginated()` (keyset `SELECT ... LIMIT`, dev `candidates`
  table — a handful of rows, not production's 1,514): **~240–250ms** per
  page, first page and a cursor-continued page alike.

These dev-connection timings are dominated by the pooled Neon endpoint's
own per-round-trip latency (the same ~230–260ms floor shows up on nearly
every single-statement query against it, regardless of what the statement
does) — they confirm the new code executes correctly against real
Postgres, not that any of these operations are themselves slow or fast in
an absolute sense. No production timing was collected for Phase 3's new
code paths, since none of them replace a path that had a "before" number;
production verification for this phase was limited to confirming the
worker/scheduler start and the new route responds correctly (see
`docs/PRODUCTION_DEPLOYMENT_CHECKLIST.md`), not to timing.
