# Production Deployment Checklist

Written for Phase 3 (background jobs, caching, resilience, cursor
pagination) — see `docs/PERFORMANCE_IMPLEMENTATION_LOG.md` for what each
item is and why. Extend this file for future phases rather than writing a
new one.

## Infrastructure

- [ ] **Nothing new to provision.** Phase 3 added no Redis, no new
  container, no new VPS service — the job queue, worker, scheduler, circuit
  breakers, and cache are all in-process + the existing Postgres database.
  The brief's own instruction ("don't deploy new Redis/worker infra without
  separately confirming VPS capacity, health checks, backups, rollback")
  is accordingly moot this phase; kept here for whichever future phase
  does introduce new infrastructure on the shared Hetzner VPS (which also
  runs `avora` and `o2` behind the same Caddy — see the VPS Coolify memory
  note: proxy must stay `None` and live-restore must stay `true`, or all
  three apps go down together).

## Before merging to `main`

- [ ] Full backend test suite passes (`pytest`), including the DB-gated
  suites if `CIRCLE_TEST_DATABASE_URL` is set in CI.
- [ ] `app/main.py` boots cleanly against a real database (not just unit
  tests) — confirm the startup log shows:
  - `Job worker started for types: [...]`
  - `OnGrid sync scheduler started (every ...)` (only if
    `ONGRID_COMMUNITY_ID` is configured)
- [ ] No teammate work-in-progress was touched or bundled into this
  branch's commits (`git diff` the WIP files named in this repo's own
  session history before pushing).

## After a Coolify redeploy

- [ ] Hit `GET /api/background-jobs/{any-id}` unauthenticated → expect
  `401` (route registered, auth enforced), not `404` (route missing) —
  the same read-only signal used throughout this program to confirm new
  code is actually live.
- [ ] Check logs for the two startup lines above.
- [ ] Trigger one real calendar-cleanup-retry or OnGrid-sync path (or wait
  for the scheduler's first natural tick) and confirm a row appears in
  `background_jobs` with `status` moving queued → processing → completed.
- [ ] Watch for `curcle.resilience` warning logs (`circuit opened after N
  consecutive failures`) in the first hour — an immediately-open circuit
  on either OnGrid or Google Calendar means a config/credentials problem
  surfaced faster than it would have before this phase, not a regression.

## Rollback

- Pure code rollback (revert the merge commit / redeploy the previous
  image) is safe: the new `background_jobs` table is additive and unused
  by old code, so rolling back loses in-flight background jobs (they
  simply never get processed) but does not corrupt or require migrating
  any existing data. No destructive migration was introduced this phase.
- If the job table itself needs to be dropped for some reason: `DROP TABLE
  IF EXISTS "background_jobs"` is safe at any time the worker/scheduler are
  stopped (i.e. during a deploy) — nothing else references it via a
  foreign key.

## Monitoring follow-ups (not implemented this phase — noted for later)

- No metrics/alerting on job queue depth, dead-letter (`failed`) count, or
  circuit-breaker open/half-open transitions beyond the structured log
  lines already emitted. If the queue starts silently backing up in
  production, the first signal today is a human noticing stale OnGrid
  statuses or a `GET /api/background-jobs/{id}` showing `retrying` forever
  — an operator dashboard over `background_jobs` (a simple `SELECT status,
  count(*) FROM background_jobs GROUP BY status`) would close this gap
  cheaply without needing new infrastructure.
