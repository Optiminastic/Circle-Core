"""What an error response is allowed to say.

Public portals are used by candidates: a token in a URL is the only credential,
and the person on the page does not work here. Several of our internal failure
messages name a table, a record id, or an environment variable to set. Those
are useful in a log and wrong in front of a candidate.

The rule is enforced at the handler, not at each `raise`, so a raise site added
later cannot get round it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import (
    SERVER_ERROR_DETAIL,
    AppError,
    AuthError,
    NotFoundError,
    RateLimitedError,
    RepositoryError,
    StorageError,
    ValidationError,
    register_exception_handlers,
)

#: Real messages from the codebase, each reachable by a candidate during an
#: upload. Every one of them names something they should never be shown.
LEAKY = [
    RepositoryError("Failed to save 'doc_requests/abc123'"),
    RepositoryError("Database is not configured. Set DATABASE_URL and restart."),
    StorageError("Document storage is not configured. Set the AWS_* env vars and restart."),
    StorageError("Failed to create a download link"),
]


def _client(exc: Exception) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_a_server_error_never_names_a_table_a_record_or_an_env_var() -> None:
    for exc in LEAKY:
        res = _client(exc).get("/boom")
        body = res.json()
        assert res.status_code >= 500
        assert body["detail"] == SERVER_ERROR_DETAIL, exc
        # The specific wording is gone, not merely wrapped.
        for giveaway in ("doc_requests", "DATABASE_URL", "AWS_", "abc123"):
            assert giveaway not in body["detail"]


def test_the_machine_readable_code_survives() -> None:
    # The sentence is generalised; the code is not, so a caller can still tell
    # a storage outage from a database one without being told the details.
    assert _client(StorageError("Failed to store the file")).get("/boom").json()["error"] == (
        "storage_error"
    )


def test_a_message_written_for_the_candidate_is_kept() -> None:
    # The whole point of a 4xx: it says what they can do about it. Generalising
    # these would replace every useful message with a shrug.
    said = "Please tick the background-verification consent before uploading your documents."
    res = _client(ValidationError(said)).get("/boom")
    assert res.status_code == 422
    assert res.json()["detail"] == said


def test_every_client_error_keeps_its_own_wording() -> None:
    for exc, status in (
        (NotFoundError("This upload link is invalid."), 404),
        (AuthError("You cannot approve your own document."), 403),
        (RateLimitedError("Too many attempts. Try again in a minute."), 429),
    ):
        res = _client(exc).get("/boom")
        assert res.status_code == status
        assert res.json()["detail"] == exc.detail  # type: ignore[attr-defined]


def test_an_unexpected_exception_says_nothing_at_all() -> None:
    res = _client(RuntimeError("psycopg.OperationalError: password authentication failed")).get(
        "/boom"
    )
    assert res.status_code == 500
    assert res.json() == {"error": "internal_error", "detail": "Internal server error"}


def test_a_bare_app_error_does_not_fall_back_to_its_docstring() -> None:
    # `AppError.__init__` defaults `detail` to the class docstring, which is
    # written for whoever reads the code. At 500 it must not reach the client.
    class ObscureFailure(AppError):
        """The write-ahead log could not be flushed to the replica."""

        status_code = 503
        code = "obscure"

    body = _client(ObscureFailure()).get("/boom").json()
    assert body["detail"] == SERVER_ERROR_DETAIL
    assert "write-ahead" not in body["detail"]
