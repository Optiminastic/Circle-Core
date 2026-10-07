"""One-off: move older employees onto the company mailbox HR recorded at
onboarding step 6. See app/services/company_email_backfill.py for the rules.

Dry run (default) prints the plan and changes nothing:

    python -m scripts.backfill_company_emails

Apply it:

    python -m scripts.backfill_company_emails --apply

The applied-with address is kept as `personalEmail`. Each move is written to
the audit log and, when id-sync is configured, queued for it like an HR edit.
"""

from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.db.database import Database
from app.repositories.audit_repository import AuditRepository
from app.repositories.document_repository import SqlAlchemyDocumentRepository
from app.repositories.identity_outbox_repository import IdentityOutboxRepository
from app.services.audit_service import AuditService
from app.services.company_email_backfill import (
    BackfillPlan,
    EmailMove,
    apply_move,
    plan_backfill,
)
from app.services.identity_sync import IdentitySyncService

EMPLOYEES = "employees"
ONBOARDING = "onboarding"
DEFAULT_DOMAIN = "optiminastic.com"
ACTOR = {"email": "system", "name": "Company email backfill", "role": "system"}


def _print_plan(plan: BackfillPlan) -> None:
    print(f"Will move {len(plan.moves)} employee(s) to their company email:")
    for m in plan.moves:
        print(f"  {m.employee_id:<10} {m.name:<28} {m.old_email} -> {m.new_email}")
    print(f"Cannot fix automatically ({len(plan.skips)}), HR must set these by hand:")
    for s in plan.skips:
        print(f"  {s.employee_id:<10} {s.name:<28} {s.email}  ({s.reason})")


def _apply(repo: SqlAlchemyDocumentRepository, move: EmailMove, audit: AuditService,
           identity_sync: IdentitySyncService | None) -> bool:
    current = repo.get(EMPLOYEES, move.employee_id)
    # Re-read right before writing: skip anyone HR edited since the plan was made.
    if current is None or str(current.get("email") or "").strip().lower() != move.old_email:
        print(f"  skipped {move.employee_id}: changed since the plan was made")
        return False
    updated = repo.upsert(EMPLOYEES, move.employee_id, apply_move(current, move))
    audit.record(
        actor=ACTOR,
        action="employee.email_backfill",
        summary=f"Moved {move.name or move.employee_id} to their company email",
        entity_type="employee",
        entity_id=move.employee_id,
        entity_label=move.name,
        metadata={"to": move.new_email},
    )
    if identity_sync is not None:
        identity_sync.enqueue_employee(updated)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Move older employees onto their company email.")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="company email domain")
    args = parser.parse_args()

    settings = get_settings()
    database = Database(settings)
    database.connect()
    session = database.session()
    try:
        repo = SqlAlchemyDocumentRepository(session)
        plan = plan_backfill(repo.find(EMPLOYEES, {}), repo.find(ONBOARDING, {}), domain=args.domain)
        _print_plan(plan)
        if not args.apply:
            print("Dry run: nothing changed. Re-run with --apply to write.")
            return
        audit = AuditService(AuditRepository(session))
        identity_sync = (
            IdentitySyncService(IdentityOutboxRepository(session)) if settings.has_identity_sync else None
        )
        done = sum(_apply(repo, move, audit, identity_sync) for move in plan.moves)
        print(f"Moved {done} of {len(plan.moves)}.")
    finally:
        session.close()
        database.dispose()


if __name__ == "__main__":
    main()
