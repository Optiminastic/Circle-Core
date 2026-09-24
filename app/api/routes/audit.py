"""Admin-only, read-only Audit Trails API.

Backs the Audit Trails page in the dashboard: a filterable feed of key HR actions
plus a per-HR summary. Append-only by construction - there is deliberately no
write / update / delete endpoint here; events are recorded server-side by
`AuditService` at the point each action happens (see curcle-be CLAUDE.md 5.6).

Every route is gated by `require_admin`: only administrators may read what HR
staff have been doing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_audit_service, require_admin
from app.services.audit_service import AuditService

router = APIRouter(prefix="/api/audit", tags=["audit"], dependencies=[Depends(require_admin)])


@router.get("/events")
def list_events(
    actor: str | None = None,
    action: str | None = None,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    audit: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    """Reverse-chronological feed of key HR actions, with optional filters.

    `date_from` / `date_to` are ISO 8601 UTC timestamps (the frontend converts the
    picked local day to a UTC start/end). `actor` filters by an HR user's email,
    `action` by an action key (e.g. `email.sent`), `q` is a free-text match.
    """
    return audit.feed(
        actor=actor, action=action, date_from=date_from, date_to=date_to, q=q, limit=limit, offset=offset
    )


@router.get("/summary")
def summary(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    audit: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    """Per-HR and per-action counts over the given window (both nullable)."""
    return audit.summary(date_from=date_from, date_to=date_to)
