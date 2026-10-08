"""Background-verification consent: what counts, and who has to give it.

Pure functions, so unlike the other suites here these need no database.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.routes.doc_requests import has_consent, needs_consent


@pytest.mark.parametrize(
    "consent",
    [
        None,
        {},
        {"agreed": True},  # ticked, but the wording was never recorded
        {"agreed": True, "text": "   "},  # whitespace is not wording
        {"agreed": False, "text": "I agree."},  # shown and declined
    ],
)
def test_incomplete_consent_is_no_consent(consent: dict[str, Any] | None) -> None:
    assert has_consent({"consent": consent}) is False


def test_a_tick_with_the_recorded_wording_is_consent() -> None:
    assert has_consent({"consent": {"agreed": True, "text": "I agree."}}) is True


@pytest.mark.parametrize("entity_type", ["candidate", None, ""])
def test_candidate_requests_need_consent(entity_type: str | None) -> None:
    # Requests predating `entityType` carry no value at all and are candidates.
    assert needs_consent({"entityType": entity_type}) is True
    assert needs_consent({}) is True


def test_employee_requests_do_not() -> None:
    # Already hired and already verified, so there is no verification to
    # consent to - only paperwork to collect.
    assert needs_consent({"entityType": "employee"}) is False


def test_the_ongrid_onboard_applies_the_same_rule() -> None:
    # The portal promises documents cannot be accepted without consent; the
    # onboard refuses to share without it. One definition, so they cannot drift.
    from app.api.routes import bgv_ongrid

    assert bgv_ongrid.has_consent is has_consent


# --------------------------------------------------------------------------
# The upload route itself. The portal disables the button, but the portal is
# the untrusted half - the token holder can call this endpoint directly.
# --------------------------------------------------------------------------


class _Repo:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def get(self, table: str, key: str) -> dict[str, Any]:
        return self._record

    def upsert(self, table: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("nothing should be written before consent is given")


class _Storage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.keys.append(key)


class _Upload:
    filename = "pan.pdf"
    content_type = "application/pdf"

    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _request(consent: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": "tok",
        "candidateId": "cand1",
        "entityType": "candidate",
        "requiredDocs": ["PAN Card"],
        "consent": consent,
    }


def _upload(record: dict[str, Any], storage: _Storage, data: bytes = b"") -> None:
    """Call the route directly, the way a token holder bypassing the UI would."""
    import asyncio

    from app.api.routes.doc_requests import upload_request_document
    from app.core.config import get_settings

    asyncio.run(
        upload_request_document(
            token="tok",
            docType="PAN Card",
            file=_Upload(data),  # type: ignore[arg-type]
            repo=_Repo(record),  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
            settings=get_settings(),
        )
    )


@pytest.mark.parametrize("consent", [None, {"agreed": False}, {"agreed": True}])
def test_the_upload_route_refuses_a_document_without_consent(
    consent: dict[str, Any] | None,
) -> None:
    from app.core.errors import ValidationError

    storage = _Storage()
    with pytest.raises(ValidationError) as err:
        _upload(_request(consent), storage, data=b"%PDF-1.4 real file")
    assert "consent" in str(err.value).lower()
    # The file was rejected, not stored and then rejected.
    assert storage.keys == []


def test_consent_lets_the_upload_through_to_the_file_checks() -> None:
    # Reaching "Empty file." means the consent guard passed - reading the
    # upload is the next thing the route does after it.
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError) as err:
        _upload(_request({"agreed": True, "text": "I agree."}), _Storage())
    assert "Empty file" in str(err.value)
