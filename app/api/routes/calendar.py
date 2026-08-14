"""Google Calendar sync endpoints (single shared HR account, one-way push).

The OAuth connect flow stores the shared account's refresh token in the
`google_oauth` table (row id "shared"). Event pushes look that token up, write
to Google, and persist the app-event -> google-event id mapping in
`calendar_links` so later updates/deletes target the same event.

Pushes are best-effort: any failure returns {pushed: false, reason} with HTTP
200 so the frontend scheduling flow is never blocked (same contract as
notifications.py). The work runs synchronously in the request (not a
BackgroundTask) because it needs the request-scoped DB session.
"""

from __future__ import annotations

from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.dependencies import get_google_calendar_service, get_repository
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services.google_calendar import GoogleCalendarService
from app.services.sessions import COOKIE_NAME, read_session

logger = get_logger("curcle.calendar")


def guard_calendar(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Only the Google OAuth redirect target is public (Google calls it with no
    cookie). Status, connect, and event push/delete require a dashboard session."""
    if request.url.path.rstrip("/").endswith("/oauth/callback"):
        return
    if not read_session(settings, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in.")


router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(guard_calendar)])

_OAUTH_TABLE = "google_oauth"
_OAUTH_ROW_ID = "shared"
_LINKS_TABLE = "calendar_links"
_OAUTH_STATE = "curcle-shared"


def _connection(repo: DocumentRepository) -> dict[str, Any] | None:
    return repo.get(_OAUTH_TABLE, _OAUTH_ROW_ID)


def cleanup_calendar_events(
    repo: DocumentRepository,
    service: GoogleCalendarService,
    settings: Settings,
    app_event_ids: Iterable[str],
) -> int:
    """Delete the Google Calendar events mapped to these app-event ids and drop
    their `calendar_links` rows. Google's ``sendUpdates="all"`` removes the event
    from every attendee's calendar (HR, interviewer, candidate). Best-effort — a
    missing link or a Google failure is skipped, never raised, so it can be called
    from a cascade delete without blocking it. Returns the number of links removed.
    """
    conn = _connection(repo)
    refresh_token = (conn or {}).get("refreshToken")
    removed = 0
    for app_event_id in app_event_ids:
        link = repo.get(_LINKS_TABLE, app_event_id)
        if not link or not link.get("googleEventId"):
            continue
        if refresh_token:
            try:
                service.delete_event(
                    refresh_token,
                    link.get("calendarId", settings.google_calendar_id),
                    link["googleEventId"],
                )
            except Exception:  # noqa: BLE001 - never block the caller on a Google error
                logger.warning("Could not delete calendar event for %s", app_event_id)
        repo.delete(_LINKS_TABLE, app_event_id)
        removed += 1
    return removed


@router.get("/status")
def status(
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    conn = _connection(repo)
    return {
        "configured": settings.has_google,
        "connected": bool(conn and conn.get("refreshToken")),
        "connectedEmail": (conn or {}).get("connectedEmail"),
        "calendarId": settings.google_calendar_id,
    }


@router.delete("/connection")
def disconnect(repo: DocumentRepository = Depends(get_repository)) -> dict[str, Any]:
    """Forget the connected Google account by deleting its stored refresh token.

    After this, scheduling still works but no events/Meet links are pushed until
    someone connects again. Connecting is an upsert on the single "shared" row, so
    the most recent account always wins — reconnect as HR to switch the account.
    """
    conn = _connection(repo)
    if not conn:
        return {"disconnected": False, "reason": "not_connected"}
    repo.delete(_OAUTH_TABLE, _OAUTH_ROW_ID)
    logger.info("Google Calendar disconnected (was %s)", conn.get("connectedEmail"))
    return {"disconnected": True}


@router.get("/oauth/url")
def oauth_url(
    service: GoogleCalendarService = Depends(get_google_calendar_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.has_google:
        return {"url": None, "reason": "not_configured"}
    return {"url": service.build_auth_url(state=_OAUTH_STATE)}


@router.get("/oauth/callback")
def oauth_callback(
    code: str | None = None,
    error: str | None = None,
    service: GoogleCalendarService = Depends(get_google_calendar_service),
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    target = f"{settings.frontend_url.rstrip('/')}/settings"

    def fail(reason: str) -> RedirectResponse:
        """Every failure carries a coarse reason code so the cause is visible in
        the redirect + logs (never a token/stack trace — see CLAUDE.md 5.5)."""
        return RedirectResponse(url=f"{target}?calendar=error&reason={reason}")

    # Google itself refused / the user declined consent.
    if error or not code:
        logger.warning(
            "Google OAuth aborted before exchange: error=%s code_present=%s", error, bool(code)
        )
        return fail(error or "no_code")

    try:
        result = service.exchange_code(code)
    except Exception:  # noqa: BLE001
        logger.exception("Google OAuth token exchange failed")
        return fail("exchange_failed")

    if not result.get("refresh_token"):
        # Google only returns a refresh token on first consent; prompt=consent in
        # build_auth_url forces it, but guard anyway (previously silent).
        logger.warning(
            "Google OAuth returned no refresh token (email=%s) — revoke the app's "
            "access at myaccount.google.com/permissions and reconnect.",
            result.get("email"),
        )
        return fail("no_refresh_token")

    repo.upsert(
        _OAUTH_TABLE,
        _OAUTH_ROW_ID,
        {
            "id": _OAUTH_ROW_ID,
            "refreshToken": result["refresh_token"],
            "connectedEmail": result.get("email"),
        },
    )
    logger.info("Google Calendar connected for %s", result.get("email"))
    return RedirectResponse(url=f"{target}?calendar=connected")


class PushEventIn(BaseModel):
    appEventId: str
    type: str  # 'HR Call' | 'IQ Test' | 'Assessment' | 'Interview'
    title: str
    dateTimeIso: str
    durationMin: int = 45
    notes: str | None = None
    attendeeEmail: str | None = None
    attendees: list[str] | None = None  # candidate / interviewer / HR
    location: str | None = None  # office address or meeting link
    online: bool | None = None  # True → attach a Google Meet link; False → none


@router.post("/events")
def push_event(
    payload: PushEventIn,
    service: GoogleCalendarService = Depends(get_google_calendar_service),
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.has_google:
        return {"pushed": False, "reason": "not_configured"}
    conn = _connection(repo)
    if not (conn and conn.get("refreshToken")):
        return {"pushed": False, "reason": "not_connected"}

    existing = repo.get(_LINKS_TABLE, payload.appEventId)
    google_event_id = (existing or {}).get("googleEventId")
    try:
        result = service.upsert_event(
            conn["refreshToken"],
            calendar_id=settings.google_calendar_id,
            event_type=payload.type,
            summary=payload.title,
            description=payload.notes or "",
            start_iso=payload.dateTimeIso,
            duration_min=payload.durationMin,
            attendee_email=payload.attendeeEmail,
            attendees=payload.attendees,
            location=payload.location,
            google_event_id=google_event_id,
            online=payload.online,
            request_id=payload.appEventId,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to push event %s to Google Calendar", payload.appEventId)
        return {"pushed": False, "reason": "error"}

    repo.upsert(
        _LINKS_TABLE,
        payload.appEventId,
        {
            "id": payload.appEventId,
            "googleEventId": result["id"],
            "calendarId": settings.google_calendar_id,
        },
    )
    return {
        "pushed": True,
        "meetLink": result.get("hangoutLink"),
        "googleEventId": result.get("id"),
    }


@router.delete("/events/{app_event_id}")
def delete_event(
    app_event_id: str,
    service: GoogleCalendarService = Depends(get_google_calendar_service),
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    link = repo.get(_LINKS_TABLE, app_event_id)
    if not link or not link.get("googleEventId"):
        return {"deleted": False, "reason": "no_link"}
    conn = _connection(repo)
    if not (conn and conn.get("refreshToken")):
        return {"deleted": False, "reason": "not_connected"}
    try:
        service.delete_event(
            conn["refreshToken"],
            link.get("calendarId", settings.google_calendar_id),
            link["googleEventId"],
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to delete event %s from Google Calendar", app_event_id)
        return {"deleted": False, "reason": "error"}
    repo.delete(_LINKS_TABLE, app_event_id)
    return {"deleted": True}
