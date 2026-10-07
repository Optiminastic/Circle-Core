"""id-sync push: message shape, signing, delivery and the outbox.

The outbox and router tests need a real PostgreSQL (SKIP LOCKED, JSONB) and
run only when CIRCLE_TEST_DATABASE_URL points at a disposable database - never
the one in .env. Everything else is pure and always runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest

from app.services.identity_sync import (
    SIGNATURE_HEADER,
    IdentitySyncClient,
    IdentitySyncError,
    IdentitySyncService,
    retry_delay_seconds,
    sign,
    to_push_message,
)

SECRET = "test-hr-webhook-secret"


def _employee(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": "EMP-1001",
        "fullName": "Asha Rao",
        "email": "Asha.Rao@Optiminastic.com",
        "department": "Production",
        "role": "Print Operator",
        "reportingManager": "Some Person",
        "joiningDate": "2026-09-01",
        "status": "Active",
        "personalDetails": {"panNumber": "ABCDE1234F"},
        "annualCtc": 600000,
    }
    doc.update(overrides)
    return doc


# --- message shape ----------------------------------------------------------------


def test_message_carries_the_directory_entry_only() -> None:
    message = to_push_message(_employee())
    assert message is not None
    assert message.pop("changed_at")  # when the change happened, for ordering
    assert message == {
        "employee": {
            "employee_code": "EMP-1001",
            "name": "Asha Rao",
            "email": "asha.rao@optiminastic.com",
            "designation": "Print Operator",
            "department": "Production",
            "status": "Active",
            "manager_code": None,
            "manager_name": "Some Person",
            "joining_date": "2026-09-01",
            "location": None,
        },
        "removed": False,
    }


def test_picked_manager_is_sent_by_code() -> None:
    message = to_push_message(_employee(reportingManagerId="EMP-1000", reportingManager="Rashi C"))
    assert message is not None
    assert message["employee"]["manager_code"] == "EMP-1000"
    assert message["employee"]["manager_name"] == "Rashi C"


def test_placeholder_manager_and_bad_date_are_dropped() -> None:
    message = to_push_message(_employee(reportingManager="—", joiningDate="soon", workLocation=" Mumbai "))
    assert message is not None
    entry = message["employee"]
    assert entry["manager_name"] is None and entry["joining_date"] is None
    assert entry["location"] == "Mumbai"


def test_sensitive_fields_never_leave_circle() -> None:
    body = json.dumps(to_push_message(_employee()))
    for secret_value in ("ABCDE1234F", "600000"):
        assert secret_value not in body


def test_push_and_export_send_the_same_entry() -> None:
    from app.services.directory_entry import to_directory_entry

    assert to_push_message(_employee())["employee"] == to_directory_entry(_employee())  # type: ignore[index]


def test_removed_employee_is_flagged() -> None:
    assert to_push_message(_employee(), removed=True)["removed"] is True  # type: ignore[index]


@pytest.mark.parametrize("missing", ["id", "email"])
def test_unmatchable_employee_is_skipped(missing: str) -> None:
    assert to_push_message(_employee(**{missing: ""})) is None


# --- signing and backoff ----------------------------------------------------------


def test_signature_matches_idsync_verifier() -> None:
    # Mirrors id-sync's require_circle_signature.
    body = b'{"a":1}'
    expected = hmac.new(SECRET.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sign(SECRET, 1700000000, body), expected)


def test_backoff_grows_then_caps_at_an_hour() -> None:
    assert retry_delay_seconds(0) == 30
    assert retry_delay_seconds(1) == 60
    assert retry_delay_seconds(50) == 3600


# --- HTTP client ------------------------------------------------------------------


class _FakeIdSync(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[bytes, str | None, str | None]]] = []
    status_code = 200

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append(
            (body, self.headers.get(SIGNATURE_HEADER), self.headers.get("X-Timestamp"))
        )
        self.send_response(type(self).status_code)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_idsync() -> Iterator[str]:
    _FakeIdSync.received = []
    _FakeIdSync.status_code = 200
    server = HTTPServer(("127.0.0.1", 0), _FakeIdSync)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/directory/employees/push"
    server.shutdown()


def test_client_sends_signed_body(fake_idsync: str) -> None:
    IdentitySyncClient(fake_idsync, SECRET).send({"employee": {"employee_code": "EMP-1001"}})
    (body, signature, timestamp), = _FakeIdSync.received
    assert json.loads(body) == {"employee": {"employee_code": "EMP-1001"}}
    assert timestamp is not None and abs(int(timestamp) - time.time()) < 60
    assert signature == sign(SECRET, int(timestamp), body)


def test_client_raises_on_rejection(fake_idsync: str) -> None:
    _FakeIdSync.status_code = 409
    with pytest.raises(IdentitySyncError, match="409"):
        IdentitySyncClient(fake_idsync, SECRET).send({})


def test_client_raises_when_unreachable() -> None:
    with pytest.raises(IdentitySyncError, match="unreachable"):
        IdentitySyncClient("http://127.0.0.1:9/nothing", SECRET).send({})


# --- outbox + router (real PostgreSQL) -------------------------------------------

TEST_DB_URL = os.environ.get("CIRCLE_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="CIRCLE_TEST_DATABASE_URL not set")


@pytest.fixture
def db_session() -> Iterator[Any]:
    from sqlalchemy import text

    from app.core.config import Settings
    from app.db.database import Database
    from app.repositories.identity_outbox_repository import TABLE

    database = Database(Settings(database_url=TEST_DB_URL, _env_file=None))  # type: ignore[call-arg]
    database.connect()
    database.ensure_identity_outbox()
    with database.session() as session:
        session.execute(text(f'TRUNCATE "{TABLE}"'))
        yield session
    database.dispose()


class _RecordingClient:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail

    def send(self, payload: dict[str, Any]) -> None:
        if self.fail:
            raise IdentitySyncError("id-sync answered HTTP 503")
        self.sent.append(payload)


@needs_db
def test_outbox_keeps_only_latest_state(db_session: Any) -> None:
    from app.repositories.identity_outbox_repository import IdentityOutboxRepository

    outbox = IdentityOutboxRepository(db_session)
    service = IdentitySyncService(outbox, _RecordingClient())  # type: ignore[arg-type]
    service.enqueue_employee(_employee(status="Active"))
    service.enqueue_employee(_employee(status="Offboarded"))

    client = _RecordingClient()
    assert IdentitySyncService(outbox, client).deliver_due() == 1  # type: ignore[arg-type]
    assert [m["employee"]["status"] for m in client.sent] == ["Offboarded"]
    assert outbox.claim_due(limit=10, lease_seconds=60) == []


@needs_db
def test_claimed_rows_are_hidden_from_other_workers(db_session: Any) -> None:
    from app.repositories.identity_outbox_repository import IdentityOutboxRepository

    outbox = IdentityOutboxRepository(db_session)
    IdentitySyncService(outbox).enqueue_employee(_employee())
    assert len(outbox.claim_due(limit=10, lease_seconds=60)) == 1
    assert outbox.claim_due(limit=10, lease_seconds=60) == []


@needs_db
def test_change_during_delivery_is_not_lost(db_session: Any) -> None:
    from app.repositories.identity_outbox_repository import IdentityOutboxRepository

    outbox = IdentityOutboxRepository(db_session)
    service = IdentitySyncService(outbox)
    service.enqueue_employee(_employee(status="Active"))
    (claimed,) = outbox.claim_due(limit=10, lease_seconds=60)
    service.enqueue_employee(_employee(status="Offboarded"))  # lands mid-delivery
    # The newer version must NOT be sendable while the older one is on the
    # wire (a second worker could deliver it first and be overwritten).
    assert outbox.claim_due(limit=10, lease_seconds=60) == []
    outbox.mark_delivered(claimed)

    (pending,) = outbox.claim_due(limit=10, lease_seconds=60)
    assert pending.payload["employee"]["status"] == "Offboarded"


@needs_db
def test_newer_change_released_after_failed_older_send(db_session: Any) -> None:
    from app.repositories.identity_outbox_repository import IdentityOutboxRepository

    outbox = IdentityOutboxRepository(db_session)
    service = IdentitySyncService(outbox)
    service.enqueue_employee(_employee(status="Active"))
    (claimed,) = outbox.claim_due(limit=10, lease_seconds=60)
    service.enqueue_employee(_employee(status="Offboarded"))
    outbox.mark_failed(claimed, error="boom", retry_in_seconds=3600)
    (pending,) = outbox.claim_due(limit=10, lease_seconds=60)
    assert pending.payload["employee"]["status"] == "Offboarded"


@needs_db
def test_failed_delivery_is_retried_later(db_session: Any) -> None:
    from sqlalchemy import text

    from app.repositories.identity_outbox_repository import TABLE, IdentityOutboxRepository

    outbox = IdentityOutboxRepository(db_session)
    IdentitySyncService(outbox).enqueue_employee(_employee())
    failing = IdentitySyncService(outbox, _RecordingClient(fail=True))  # type: ignore[arg-type]
    assert failing.deliver_due() == 0

    row = db_session.execute(
        text(f'SELECT attempts, last_error, next_attempt_at > now() AS later FROM "{TABLE}"')
    ).one()
    assert row.attempts == 1
    assert "503" in row.last_error
    assert row.later


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, list[tuple[dict[str, Any], bool]]]]:
    from fastapi.testclient import TestClient

    from app.api.dependencies import current_user, get_identity_sync
    from app.api.routes.resources import guard_resources
    from app.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("IDSYNC_PUSH_URL", "")  # worker off; hooks are captured below
    get_settings.cache_clear()
    from app.main import create_app

    queued: list[tuple[dict[str, Any], bool]] = []

    class _Recorder:
        def enqueue_employee(self, doc: dict[str, Any], *, removed: bool = False) -> None:
            queued.append((doc, removed))

    app = create_app()
    app.dependency_overrides[guard_resources] = lambda: None
    app.dependency_overrides[current_user] = lambda: {"email": "hr@corp.io", "role": "hr", "name": "HR"}
    app.dependency_overrides[get_identity_sync] = lambda: _Recorder()
    with TestClient(app) as client:
        client.delete("/api/employees/EMP-9001")
        queued.clear()
        yield client, queued
    get_settings.cache_clear()


@needs_db
def test_every_employee_write_is_queued(app_client: Any) -> None:
    client, queued = app_client
    assert client.post("/api/employees", json=_employee(id="EMP-9001")).status_code == 201
    assert client.patch("/api/employees/EMP-9001", json={"status": "Offboarded"}).status_code == 200
    put = client.put("/api/employees/EMP-9001", json=_employee(id="EMP-9001", status="Active"))
    assert put.status_code == 200
    assert client.delete("/api/employees/EMP-9001").status_code == 204

    assert [(doc["status"], removed) for doc, removed in queued] == [
        ("Active", False),
        ("Offboarded", False),
        ("Active", False),
        ("Active", True),
    ]


@needs_db
def test_other_resources_are_not_queued(app_client: Any) -> None:
    client, queued = app_client
    client.post("/api/jobs", json={"id": "job-idsync-test", "title": "x"})
    client.delete("/api/jobs/job-idsync-test")
    assert queued == []
