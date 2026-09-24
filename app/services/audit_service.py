"""Records and reads the HR activity audit trail.

`record` is best-effort and MUST NEVER raise: an audit-write failure must not
break the HR action that triggered it. Every call is wrapped and logged instead.
The read helpers (`feed`, `summary`) back the admin-only Audit Trails page.

Only "key actions" are recorded (candidate added / moved / rejected / deleted,
emails sent, employee onboarded, offboarding started, logins, account changes) -
not every field edit - and only non-sensitive descriptors are stored: an actor,
an action, a human summary and an entity label. No salary, PAN, Aadhaar or bank
value ever enters this log.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.core.logging import get_logger
from app.repositories.audit_repository import AuditRepository

logger = get_logger("curcle.audit")


class AuditService:
    def __init__(self, repo: AuditRepository) -> None:
        self._repo = repo

    def record(
        self,
        *,
        actor: dict[str, Any] | None,
        action: str,
        summary: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        entity_label: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one key-action event. Never raises."""
        try:
            a = actor or {}
            data = {
                "actor_email": a.get("email"),
                "actor_name": a.get("name") or a.get("email"),
                "actor_role": a.get("role"),
                "action": action,
                "summary": summary,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_label": entity_label,
                "metadata": metadata or {},
            }
            self._repo.insert(uuid4().hex, data)
        except Exception:  # noqa: BLE001 - audit must never break the primary action
            logger.exception("Failed to record audit event '%s'.", action)

    def feed(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        events = self._repo.query(
            actor=actor, action=action, date_from=date_from, date_to=date_to, q=q, limit=limit, offset=offset
        )
        total = self._repo.count(actor=actor, action=action, date_from=date_from, date_to=date_to, q=q)
        return {"events": events, "total": total, "limit": limit, "offset": offset}

    def summary(self, *, date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
        return self._repo.summary(date_from=date_from, date_to=date_to)
