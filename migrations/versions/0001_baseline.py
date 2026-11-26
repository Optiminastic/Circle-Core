"""Baseline — documents the schema as it already exists.

Revision ID: 0001
Revises:
Create Date: 2026-11-26

**For every existing database (dev, staging, production): run
`alembic stamp 0001`, never `alembic upgrade head` from empty.** The tables
below already exist — created at boot by `Database.ensure_tables()`
(app/db/database.py), which this migration's `upgrade()` deliberately
mirrors rather than importing (a migration should be a frozen historical
snapshot, not a live reference to app code that can change later). Running
`upgrade` against a database that already has these tables is harmless
either way (every statement is `IF NOT EXISTS`), but `stamp` is correct and
doesn't take a redundant lock on every table to find that out.

A genuinely brand-new, empty database should still go through the app's own
first-boot `ensure_tables()` (`auto_create_tables=True`) — that bootstrap
remains authoritative for the generic-resource table shape, not this
migration, which only has to be the resource list as of the day this was
written. See docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 2 for why the two
coexist: this phase adds Alembic for new, deliberate, reviewed changes
(starting with 0002's indexes) without disturbing how base tables already
come into existence.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Every table registered as a generic resource as of this migration (see
#: app/domain/registry.py) plus the ones ensure_tables() adds explicitly
#: (app/main.py's lifespan): documents, email_otps, audit_events,
#: screening_calls. A table added to the registry AFTER this migration was
#: written is not retroactively added here — that's what the NEXT migration
#: is for, same as any other schema change.
_GENERIC_RESOURCE_TABLES: tuple[str, ...] = (
    "auth_users", "schedules", "jobs", "candidates", "test_invites", "doc_requests",
    "interviews", "iq_tests", "assignments", "bgvs", "onboarding", "employees", "assets",
    "email_templates", "email_template_overrides", "sent_emails", "offboarding",
    "exit_handovers", "candidate_handoffs", "google_oauth", "calendar_links",
    "assessment_banks", "interview_banks", "screening_banks", "iq_bank",
    "assignment_banks", "interview_sheets", "interview_kit_sends", "candidate_portals",
    "joining_confirmations",
    # Added directly in app/main.py's lifespan, not through the registry:
    "documents", "email_otps", "audit_events", "screening_calls",
)


def upgrade() -> None:
    for table in _GENERIC_RESOURCE_TABLES:
        op.execute(
            f'CREATE TABLE IF NOT EXISTS "{table}" ('
            "id TEXT PRIMARY KEY, "
            "data JSONB NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
        op.execute(f'CREATE INDEX IF NOT EXISTS "ix_{table}_created_at" ON "{table}" (created_at)')
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "ix_{table}_data_gin" ON "{table}" USING GIN (data jsonb_path_ops)'
        )

    op.execute('CREATE SEQUENCE IF NOT EXISTS "employee_code_seq" AS bigint MINVALUE 1000 START 1000')

    op.execute(
        'CREATE TABLE IF NOT EXISTS "identity_outbox" ('
        "employee_id TEXT PRIMARY KEY, "
        "payload JSONB NOT NULL, "
        "version BIGINT NOT NULL DEFAULT 1, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "in_flight BOOLEAN NOT NULL DEFAULT false, "
        "next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "last_error TEXT, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    op.execute('CREATE INDEX IF NOT EXISTS "ix_identity_outbox_due" ON "identity_outbox" (next_attempt_at)')


def downgrade() -> None:
    # Deliberately not implemented: this is the baseline of a live database
    # with real HR data — a "downgrade" would mean dropping every table.
    # There is no safe automatic downgrade from here.
    raise RuntimeError(
        "Refusing to downgrade past the baseline — this would drop every "
        "resource table. If you genuinely need to tear down a database, "
        "do it explicitly (DROP TABLE / DROP DATABASE), not via this migration."
    )
