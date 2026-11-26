"""Functional + trigram indexes for the JSONB filter/search/sort patterns
identified in docs/PERFORMANCE_AUDIT.md P1-2/P1-3 and wired up in Phase 2's
pagination (app/domain/resource_search.py) and dashboard stats
(app/repositories/stats_repository.py).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26

None of these duplicate the existing `GIN (data jsonb_path_ops)` index every
table already has (created in 0001) — that index accelerates `@>`
containment only; it does nothing for `->>` text extraction, `ILIKE`, or
`GROUP BY`, which is exactly what every index below is for.

**Not executed by this session.** Per the brief's own instruction ("test
migrations against a production-like database copy... make migration
application a controlled deployment step") and this project's standing rule
not to run schema-modifying operations without explicit approval (the same
line this session already hit once in Phase 0 over `pg_stat_statements`),
this migration is reviewed code only until you say to run it. See
docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 2 for the exact commands and
what each index costs to maintain on writes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Functional indexes for exact-match ->> lookups (no trigram needed) ---

    # avora_internal.py's employee-by-email lookup (find_text_ci) — called by
    # the OTHER app (avora) sharing this VPS; was a full sequential scan.
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_employees_email_ci" '
        'ON "employees" (lower(trim(data->>\'email\')))'
    )

    # audit_repository.py's actor/action filters and GROUP BY — admin-facing,
    # lower traffic, but the audit table only ever grows.
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_audit_events_actor_email" '
        'ON "audit_events" ((data->>\'actor_email\'))'
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_audit_events_action" '
        'ON "audit_events" ((data->>\'action\'))'
    )

    # stats_repository.py's upcoming_interviews — a partial index (WHERE
    # status = 'Scheduled') since that's the only state this query ever asks
    # for, so the index stays small as old/completed interviews accumulate.
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_interviews_scheduled_datetime" '
        'ON "interviews" (((data->>\'dateTime\')::timestamptz)) '
        "WHERE data->>'status' = 'Scheduled'"
    )

    # --- Trigram indexes for ILIKE search (app/domain/resource_search.py) ---
    # One extension enables trigram ops for every index below.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # The single highest-value search field per resource Phase 2 wired up
    # pagination search for. Each resource's remaining search fields
    # (app/domain/resource_search.SEARCH_FIELDS) still work without an index —
    # just as a sequential scan, same as before this migration — add the same
    # kind of index for any of them if EXPLAIN on real traffic shows it's
    # worth it; this migration doesn't index every field pre-emptively.
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_candidates_fullname_trgm" '
        "ON \"candidates\" USING GIN ((data->>'fullName') gin_trgm_ops)"
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_employees_fullname_trgm" '
        "ON \"employees\" USING GIN ((data->>'fullName') gin_trgm_ops)"
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_jobs_title_trgm" '
        "ON \"jobs\" USING GIN ((data->>'title') gin_trgm_ops)"
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_interviews_candidatename_trgm" '
        "ON \"interviews\" USING GIN ((data->>'candidateName') gin_trgm_ops)"
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS "ix_doc_requests_candidatename_trgm" '
        "ON \"doc_requests\" USING GIN ((data->>'candidateName') gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS "ix_doc_requests_candidatename_trgm"')
    op.execute('DROP INDEX IF EXISTS "ix_interviews_candidatename_trgm"')
    op.execute('DROP INDEX IF EXISTS "ix_jobs_title_trgm"')
    op.execute('DROP INDEX IF EXISTS "ix_employees_fullname_trgm"')
    op.execute('DROP INDEX IF EXISTS "ix_candidates_fullname_trgm"')
    op.execute('DROP INDEX IF EXISTS "ix_interviews_scheduled_datetime"')
    op.execute('DROP INDEX IF EXISTS "ix_audit_events_action"')
    op.execute('DROP INDEX IF EXISTS "ix_audit_events_actor_email"')
    op.execute('DROP INDEX IF EXISTS "ix_employees_email_ci"')
    # pg_trgm is left installed on downgrade — other indexes/extensions may
    # depend on it, and dropping an extension is its own deliberate decision.
