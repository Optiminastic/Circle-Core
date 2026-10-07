"""Push each employee change to id-sync, the shared identity service.

Circle is the HR source of truth. Every employee write queues the employee's
directory entry (see `IdentityOutboxRepository`) and `IdentitySyncService.
deliver_due` sends the queue to id-sync's POST /directory/employees/push, which
updates the registry and passes the change on to Keycloak and Avora. id-sync
also pulls the full roster on a schedule, so anything lost here still converges.

Only the directory entry crosses the boundary - exactly what
/api/directory/export serves. The body is signed with INTERNAL_API_SECRET (the
secret id-sync already uses to read that export), so the secret itself never
travels with the request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.repositories.identity_outbox_repository import IdentityOutboxRepository, OutboxItem
from app.services.directory_entry import to_directory_entry

logger = get_logger("curcle.identity_sync")

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"
# Cloudflare-style proxies reject urllib's default agent (see email_sender.py).
_USER_AGENT = "circle-identity-sync/1.0"
_REQUEST_TIMEOUT_SECONDS = 10

CLAIM_BATCH_SIZE = 10
# Longer than a worst-case batch (10 x ~20s per slow request), so a row is never
# re-claimed while still being sent; a crashed worker's rows reappear after this.
CLAIM_LEASE_SECONDS = 600
_RETRY_BASE_SECONDS = 30
_RETRY_MAX_SECONDS = 3600


def to_push_message(doc: dict[str, Any], *, removed: bool = False) -> dict[str, Any] | None:
    """The id-sync push body, or None when the employee has no email or code.

    `removed` marks an employee deleted from Circle; id-sync records them as
    departed and never deletes the identity.
    """
    entry = to_directory_entry(doc)
    if entry is None:
        return None
    # When the change happened, so id-sync can ignore an older change that
    # arrives after a newer one.
    changed_at = datetime.now(timezone.utc).isoformat()
    return {"employee": entry, "removed": removed, "changed_at": changed_at}


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Hex HMAC-SHA256 of "<timestamp>.<body>", as id-sync's push endpoint
    expects: a captured request stops being accepted after a few minutes."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def retry_delay_seconds(attempts: int) -> int:
    """Exponential backoff from 30s, capped at an hour. Never gives up: the next
    edit to the employee re-queues it with a fresh attempt count."""
    return min(_RETRY_BASE_SECONDS * 2 ** min(attempts, 16), _RETRY_MAX_SECONDS)


class IdentitySyncError(Exception):
    """Delivery failed; the message is safe to store and log (no payload)."""


class IdentitySyncClient:
    def __init__(self, url: str, secret: str) -> None:
        self._url = url
        self._secret = secret

    def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, separators=(",", ":")).encode()
        timestamp = int(time.time())
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
                TIMESTAMP_HEADER: str(timestamp),
                SIGNATURE_HEADER: sign(self._secret, timestamp, body),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS):
                return
        except urllib.error.HTTPError as exc:
            raise IdentitySyncError(f"id-sync answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IdentitySyncError(f"id-sync unreachable: {exc}") from exc


class IdentitySyncService:
    def __init__(
        self, outbox: IdentityOutboxRepository, client: IdentitySyncClient | None = None
    ) -> None:
        self._outbox = outbox
        self._client = client

    def enqueue_employee(self, doc: dict[str, Any], *, removed: bool = False) -> None:
        """Queue an employee's current state. Never raises: the HR write that
        triggered it has already succeeded, and id-sync's scheduled pull
        repairs anything lost here."""
        try:
            message = to_push_message(doc, removed=removed)
            if message is None:
                logger.warning("Employee %s has no code or email; not pushed to id-sync.", doc.get("id"))
                return
            self._outbox.enqueue(message["employee"]["employee_code"], message)
        except Exception:
            logger.exception("Failed to queue employee %s for id-sync.", doc.get("id"))

    def deliver_due(self) -> int:
        """Send one batch of due rows. Returns how many were delivered."""
        client = self._client
        if client is None:
            return 0
        items = self._outbox.claim_due(limit=CLAIM_BATCH_SIZE, lease_seconds=CLAIM_LEASE_SECONDS)
        return sum(self._deliver(client, item) for item in items)

    def _deliver(self, client: IdentitySyncClient, item: OutboxItem) -> bool:
        try:
            client.send(item.payload)
        except IdentitySyncError as exc:
            delay = retry_delay_seconds(item.attempts)
            logger.warning(
                "id-sync push for %s failed (attempt %d), retrying in %ds: %s",
                item.employee_id, item.attempts + 1, delay, exc,
            )
            self._outbox.mark_failed(item, error=str(exc), retry_in_seconds=delay)
            return False
        self._outbox.mark_delivered(item)
        return True
