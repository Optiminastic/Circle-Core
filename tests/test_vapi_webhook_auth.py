"""Signature authentication on the Vapi webhook.

What an accepted event can do is the reason these matter: it writes a fit
rating, the answers, the transcript and a recording URL onto a real candidate.

Pure checks need no app; the HTTP ones reuse the stub service from
test_screening_calls_api so nothing touches a database or the network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_user, get_screening_call_service
from app.core.config import get_settings
from app.services.webhook_auth import (
    SeenSignatures,
    expected_signature,
    signed_payload,
    verify_signature,
)
from tests.test_screening_calls_api import HR, StubService

SIGNING_SECRET = "a-signing-secret"
BODY = b'{"message":{"type":"end-of-call-report"}}'


def sign(body: bytes, secret: str = SIGNING_SECRET, timestamp: str | None = None) -> str:
    return hmac.new(secret.encode(), signed_payload(body, timestamp), hashlib.sha256).hexdigest()


# -- the rule itself ---------------------------------------------------------


def test_a_correct_signature_over_the_body_is_accepted() -> None:
    check = verify_signature(
        body=BODY, signature=sign(BODY), timestamp=None, secret=SIGNING_SECRET,
        require_timestamp=False,
    )
    assert check.ok is True


def test_a_changed_body_no_longer_matches() -> None:
    signature = sign(BODY)
    tampered = BODY.replace(b"end-of-call-report", b"status-update")
    check = verify_signature(
        body=tampered, signature=signature, timestamp=None, secret=SIGNING_SECRET,
        require_timestamp=False,
    )
    assert check.ok is False and "does not match" in check.reason


def test_another_key_does_not_pass() -> None:
    check = verify_signature(
        body=BODY, signature=sign(BODY, "someone-elses-secret"), timestamp=None,
        secret=SIGNING_SECRET, require_timestamp=False,
    )
    assert check.ok is False


@pytest.mark.parametrize("signature", [None, "", "not-hex", "deadbeef"])
def test_a_missing_or_junk_signature_is_refused(signature: str | None) -> None:
    assert not verify_signature(
        body=BODY, signature=signature, timestamp=None, secret=SIGNING_SECRET,
        require_timestamp=False,
    )


def test_a_sha256_prefix_is_tolerated() -> None:
    # Several senders write "sha256=<hex>"; refusing that looks exactly like a
    # wrong key while being a far duller problem.
    check = verify_signature(
        body=BODY, signature=f"sha256={sign(BODY)}", timestamp=None, secret=SIGNING_SECRET,
        require_timestamp=False,
    )
    assert check.ok is True


def test_nothing_passes_when_no_secret_is_configured() -> None:
    assert not verify_signature(
        body=BODY, signature=sign(BODY), timestamp=None, secret="", require_timestamp=False,
    )


# -- freshness ---------------------------------------------------------------


def test_a_recent_timestamped_request_is_accepted() -> None:
    now = 1_760_000_000.0
    ts = str(int(now))
    check = verify_signature(
        body=BODY, signature=sign(BODY, timestamp=ts), timestamp=ts, secret=SIGNING_SECRET,
        require_timestamp=True, now=now,
    )
    assert check.ok is True


def test_a_captured_request_stops_working_once_it_is_stale() -> None:
    now = 1_760_000_000.0
    ts = str(int(now - 3600))          # signed an hour ago, signature still valid
    check = verify_signature(
        body=BODY, signature=sign(BODY, timestamp=ts), timestamp=ts, secret=SIGNING_SECRET,
        require_timestamp=True, now=now,
    )
    assert check.ok is False and "out of date" in check.reason


def test_milliseconds_are_understood_as_well_as_seconds() -> None:
    now = 1_760_000_000.0
    ts = str(int(now * 1000))
    check = verify_signature(
        body=BODY, signature=sign(BODY, timestamp=ts), timestamp=ts, secret=SIGNING_SECRET,
        require_timestamp=True, now=now,
    )
    assert check.ok is True


def test_dropping_the_timestamp_does_not_get_you_the_body_only_format() -> None:
    # The attack this blocks: strip the age, present a body-only signature, and
    # the replay window comes back.
    assert not verify_signature(
        body=BODY, signature=sign(BODY), timestamp=None, secret=SIGNING_SECRET,
        require_timestamp=True,
    )


@pytest.mark.parametrize("timestamp", ["yesterday", "", "NaN"])
def test_an_unreadable_timestamp_is_refused(timestamp: str) -> None:
    assert not verify_signature(
        body=BODY, signature=sign(BODY, timestamp=timestamp), timestamp=timestamp,
        secret=SIGNING_SECRET, require_timestamp=True,
    )


def test_the_timestamp_is_covered_by_the_signature() -> None:
    # Moving the clock forward on a captured request must invalidate it.
    now = 1_760_000_000.0
    old = str(int(now - 3600))
    check = verify_signature(
        body=BODY, signature=sign(BODY, timestamp=old), timestamp=str(int(now)),
        secret=SIGNING_SECRET, require_timestamp=True, now=now,
    )
    assert check.ok is False and "does not match" in check.reason


# -- replay ------------------------------------------------------------------


def test_a_signature_is_accepted_once() -> None:
    seen = SeenSignatures()
    signature = sign(BODY)
    assert seen.check_and_remember(signature) is True
    assert seen.check_and_remember(signature) is False


def test_two_different_events_do_not_collide() -> None:
    seen = SeenSignatures()
    assert seen.check_and_remember(sign(BODY)) is True
    assert seen.check_and_remember(sign(BODY + b" ")) is True


def test_the_cache_does_not_grow_for_ever() -> None:
    seen = SeenSignatures(ttl_seconds=60)
    seen.check_and_remember("old", now=1_000.0)
    seen.check_and_remember("new", now=1_100.0)   # evicts anything before 1_040
    assert "old" not in seen._seen
    # And past the window the timestamp check is what refuses it, not this.
    assert seen.check_and_remember("old", now=1_100.0) is True


# -- over HTTP ---------------------------------------------------------------


def make_client(monkeypatch: pytest.MonkeyPatch, stub: StubService, **env: str) -> TestClient:
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "hook-secret")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_screening_call_service] = lambda: stub
    app.dependency_overrides[current_user] = lambda: HR
    return TestClient(app)


@pytest.fixture
def stub() -> StubService:
    return StubService()


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _forget_seen_signatures() -> Iterator[None]:
    """The replay cache outlives an app instance on purpose, so each test has
    to start from empty or they poison each other."""
    from app.api.routes import vapi_webhook

    vapi_webhook._seen._seen.clear()
    yield
    vapi_webhook._seen._seen.clear()


def _post(client: TestClient, body: dict[str, Any], **headers: str):
    raw = json.dumps(body).encode()
    return client.post(
        "/api/vapi/webhook",
        content=raw,
        headers={"content-type": "application/json", **headers},
    ), raw


def test_the_shared_secret_still_works_when_no_signing_secret_is_set(
    monkeypatch: pytest.MonkeyPatch, stub: StubService
) -> None:
    # The switch to HMAC happens in Vapi's dashboard; until then this is the
    # live path and must not break.
    client = make_client(monkeypatch, stub)
    response, _ = _post(client, {"message": {"type": "status-update"}}, **{"x-vapi-secret": "hook-secret"})
    assert response.status_code == 200
    assert len(stub.events) == 1


def test_the_shared_secret_is_ignored_once_signing_is_configured(
    monkeypatch: pytest.MonkeyPatch, stub: StubService
) -> None:
    # Otherwise configuring HMAC would add a door rather than replace one.
    client = make_client(monkeypatch, stub, VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET)
    response, _ = _post(client, {"message": {"type": "status-update"}}, **{"x-vapi-secret": "hook-secret"})
    assert response.status_code == 401
    assert stub.events == []


def test_a_signed_request_is_applied(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(
        monkeypatch, stub,
        VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
        VAPI_WEBHOOK_TIMESTAMP_HEADER="",
    )
    body = {"message": {"type": "end-of-call-report"}}
    raw = json.dumps(body).encode()
    response = client.post(
        "/api/vapi/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-signature": sign(raw)},
    )
    assert response.status_code == 200
    assert len(stub.events) == 1


def test_a_forged_report_is_refused(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
                         VAPI_WEBHOOK_TIMESTAMP_HEADER="")
    forged = {"message": {"type": "end-of-call-report", "analysis": {"structuredData": {}}}}
    raw = json.dumps(forged).encode()
    response = client.post(
        "/api/vapi/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-signature": sign(b"a different body")},
    )
    assert response.status_code == 401
    assert stub.events == [], "nothing may reach the candidate's record"


def test_replaying_a_valid_request_is_refused(
    monkeypatch: pytest.MonkeyPatch, stub: StubService
) -> None:
    client = make_client(monkeypatch, stub, VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
                         VAPI_WEBHOOK_TIMESTAMP_HEADER="")
    raw = json.dumps({"message": {"type": "end-of-call-report"}}).encode()
    headers = {"content-type": "application/json", "x-signature": sign(raw)}
    first = client.post("/api/vapi/webhook", content=raw, headers=headers)
    second = client.post("/api/vapi/webhook", content=raw, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(stub.events) == 1


def test_the_refusal_does_not_say_what_was_wrong(
    monkeypatch: pytest.MonkeyPatch, stub: StubService
) -> None:
    client = make_client(monkeypatch, stub, VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
                         VAPI_WEBHOOK_TIMESTAMP_HEADER="")
    raw = json.dumps({"message": {"type": "status-update"}}).encode()
    response = client.post(
        "/api/vapi/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-signature": "00" * 32},
    )
    assert response.json()["detail"] == "Invalid webhook signature."


def test_the_signature_is_checked_against_the_bytes_as_sent(
    monkeypatch: pytest.MonkeyPatch, stub: StubService
) -> None:
    # Signing re-serialised JSON would reorder keys and drop the spaces, and
    # the digest would never match. This body has both.
    client = make_client(monkeypatch, stub, VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET,
                         VAPI_WEBHOOK_TIMESTAMP_HEADER="")
    raw = b'{ "message" : { "type" : "status-update" ,  "zz" : 1 } }'
    response = client.post(
        "/api/vapi/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-signature": sign(raw)},
    )
    assert response.status_code == 200


def test_a_stale_timestamped_request_is_refused_over_http(
    monkeypatch: pytest.MonkeyPatch, stub: StubService
) -> None:
    client = make_client(monkeypatch, stub, VAPI_WEBHOOK_SIGNING_SECRET=SIGNING_SECRET)
    raw = json.dumps({"message": {"type": "status-update"}}).encode()
    old = str(int(time.time() - 3600))
    response = client.post(
        "/api/vapi/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-signature": sign(raw, timestamp=old),
            "x-timestamp": old,
        },
    )
    assert response.status_code == 401
    assert stub.events == []


def test_expected_signature_matches_a_hand_rolled_hmac() -> None:
    # Guards the wire format itself: if this changes, every sender must be
    # reconfigured, so it should never change by accident.
    assert expected_signature(BODY, None, SIGNING_SECRET) == hmac.new(
        SIGNING_SECRET.encode(), BODY, hashlib.sha256
    ).hexdigest()
    assert expected_signature(BODY, "123", SIGNING_SECRET) == hmac.new(
        SIGNING_SECRET.encode(), b"123." + BODY, hashlib.sha256
    ).hexdigest()
