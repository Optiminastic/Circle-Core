"""FastAPI dependency providers — compose the object graph per request.

The concrete implementations are wired here only; routes and services depend on
abstractions. Sessions are opened per request and always closed (error tolerance).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import StorageError
from app.db.database import Database
from app.repositories.audit_repository import AuditRepository
from app.repositories.identity_outbox_repository import IdentityOutboxRepository
from app.repositories.base import DocumentRepository
from app.repositories.document_repository import SqlAlchemyDocumentRepository
from app.services.audit_service import AuditService
from app.services.identity_sync import IdentitySyncService
from app.services.google_calendar import GoogleCalendarService
from app.services.resource_service import ResourceService
from app.services.screening_call_assistant import AssistantSettings
from app.services.screening_calls import ScreeningCallService
from app.services.vapi import VapiClient
from app.services.sessions import read_session
from app.storage.base import FileStorage


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_storage(request: Request) -> FileStorage:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise StorageError("Document storage is not configured. Set the AWS_* env vars and restart.")
    return storage


def get_session(database: Database = Depends(get_database)) -> Iterator[Session]:
    session = database.session()
    try:
        yield session
    finally:
        session.close()


def get_repository(session: Session = Depends(get_session)) -> DocumentRepository:
    return SqlAlchemyDocumentRepository(session)


def get_resource_service(repo: DocumentRepository = Depends(get_repository)) -> ResourceService:
    return ResourceService(repo)


def get_audit_service(session: Session = Depends(get_session)) -> AuditService:
    # Shares the per-request session; AUTOCOMMIT means the audit insert commits
    # independently of the primary write, so it can't roll one back.
    return AuditService(AuditRepository(session))


def get_screening_call_service(
    repo: DocumentRepository = Depends(get_repository),
    audit: AuditService = Depends(get_audit_service),
    settings: Settings = Depends(get_settings),
) -> ScreeningCallService:
    """AI screening calls; provider is None until Vapi is configured (the service answers 503)."""
    configured = settings.has_vapi
    provider = (
        VapiClient(
            api_key=settings.vapi_api_key,
            public_key=settings.vapi_public_key,
            phone_number_id=settings.vapi_phone_number_id,
            base_url=settings.vapi_base_url,
        )
        if configured
        else None
    )
    assistant_settings = (
        AssistantSettings(
            webhook_url=settings.vapi_webhook_url,
            webhook_secret=settings.vapi_webhook_secret,
            bridge_url=settings.voice_bridge_url,
            bridge_secret=settings.voice_bridge_secret,
            llm_provider=settings.vapi_llm_provider,
            llm_model=settings.vapi_llm_model,
        )
        if configured
        else None
    )
    return ScreeningCallService(
        repo=repo,
        provider=provider,
        assistant_settings=assistant_settings,
        phone_enabled=settings.has_vapi_phone,
        audit=audit,
    )


def get_identity_sync(
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings)
) -> IdentitySyncService | None:
    """Queue-only sync service (delivery runs in IdentitySyncWorker); None when disabled."""
    if not settings.has_identity_sync:
        return None
    return IdentitySyncService(IdentityOutboxRepository(session))


def get_google_calendar_service(
    settings: Settings = Depends(get_settings),
) -> GoogleCalendarService:
    return GoogleCalendarService(settings)


# --- Auth ---------------------------------------------------------------------

def current_user(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict[str, Any] | None:
    """Decode the session cookie into {email, role, name}, or None if unauthenticated."""
    from app.services.sessions import COOKIE_NAME

    return read_session(settings, request.cookies.get(COOKIE_NAME))


def require_user(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in.")
    return user


def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user
