"""Google Calendar one-way push (single shared HR account).

Pure transport — no FastAPI imports. The OAuth client id/secret come from
`Settings`; the shared account's *refresh token* is obtained once via the in-app
connect flow and persisted in the DB by the route layer (never in env).

Errors are raised to the caller so the route can decide how to respond; the route
treats event pushes as best-effort (a failed push never breaks scheduling).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import httplib2
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.resilience import CircuitBreaker

# Google sometimes returns a superset of the requested scopes (e.g. it folds in
# the granted calendar scope); relax oauthlib so that does not raise on exchange.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

logger = get_logger("curcle.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Rounds that happen online get a Google Meet link; office rounds do not
# (mirrors OFFLINE_TYPES in email_sender.py).
_MEET_TYPES = {"HR Call", "Interview"}

_TIME_ZONE = "Asia/Kolkata"

#: httplib2 (the transport googleapiclient uses) has NO timeout by default,
#: so a stalled Google API call could hang indefinitely — blocking a
#: scheduling request, or a candidate-delete cascade's cleanup, for as long
#: as Google takes to respond (or forever on a dropped connection). See
#: docs/PERFORMANCE_AUDIT.md P1-5.
_HTTP_TIMEOUT_SECONDS = 15

# Process-global (per uvicorn worker). Wrapped around the token refresh
# below — every Google Calendar operation goes through _credentials() first,
# so this one chokepoint covers push, delete, and the OAuth-connect email
# lookup alike. See docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 3 C4.
_CIRCUIT = CircuitBreaker(name="google-calendar", failure_threshold=5, reset_timeout_seconds=60.0)


class GoogleCalendarService:
    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.google_client_id
        self._client_secret = settings.google_client_secret
        self._redirect_uri = settings.google_redirect_uri

    # --- OAuth (shared-account connect, runs synchronously in the request) ---

    def _client_config(self) -> dict[str, Any]:
        return {
            "web": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_uri": _AUTH_URI,
                "token_uri": _TOKEN_URI,
                "redirect_uris": [self._redirect_uri],
            }
        }

    def _flow(self) -> Flow:
        """Build a Flow for this client.

        PKCE is deliberately OFF (`autogenerate_code_verifier=False`). The consent
        URL and the token exchange happen in two separate HTTP requests, i.e. two
        different Flow objects — so a code_verifier auto-generated while building
        the consent URL is gone by the time we exchange the code, and Google then
        rejects the exchange with "invalid_grant: Missing code verifier". This is a
        confidential web client authenticated by its client_secret, for which PKCE
        is optional (it protects public clients that cannot hold a secret).
        """
        flow = Flow.from_client_config(
            self._client_config(), scopes=SCOPES, autogenerate_code_verifier=False
        )
        flow.redirect_uri = self._redirect_uri
        return flow

    def build_auth_url(self, state: str) -> str:
        """Return the Google consent URL for connecting the shared account."""
        url, _ = self._flow().authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",  # force a refresh_token even on re-consent
            state=state,
        )
        return url

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange the OAuth code for a long-lived refresh token."""
        flow = self._flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
        return {"refresh_token": creds.refresh_token, "email": self._primary_email(creds)}

    # --- authorized client ---

    def _credentials(self, refresh_token: str) -> Credentials:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=SCOPES,
        )
        # Circuit-breaker-wrapped: this one call gates every Calendar
        # operation (push, delete, OAuth-connect email lookup), so it's the
        # single chokepoint worth protecting rather than wrapping each
        # .execute() call site individually.
        _CIRCUIT.call(lambda: creds.refresh(GoogleRequest()))
        return creds

    def _service(self, refresh_token: str):
        http = AuthorizedHttp(
            self._credentials(refresh_token), http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS)
        )
        return build("calendar", "v3", http=http, cache_discovery=False)

    def _primary_email(self, creds: Credentials) -> str | None:
        """Best-effort: the primary calendar id is the account email."""
        try:
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS))
            service = build("calendar", "v3", http=http, cache_discovery=False)
            cal = service.calendars().get(calendarId="primary").execute()
            return cal.get("id")
        except Exception:  # noqa: BLE001
            logger.warning("Could not read primary calendar email", exc_info=True)
            return None

    # --- event push (create/update/delete) ---

    def upsert_event(
        self,
        refresh_token: str,
        *,
        calendar_id: str,
        event_type: str,
        summary: str,
        description: str,
        start_iso: str,
        duration_min: int,
        attendee_email: str | None = None,
        attendees: list[str] | None = None,
        location: str | None = None,
        google_event_id: str | None = None,
        online: bool | None = None,
        request_id: str,
    ) -> dict[str, Any]:
        """Create or patch a calendar event. Returns {id, hangoutLink?}.

        Attendees (candidate / interviewer / HR) are invited via Google's own
        invitation email (sendUpdates="all"). Both the legacy single
        ``attendee_email`` and the ``attendees`` list are accepted and merged.

        A Google Meet link is attached when ``online`` is True (the explicit
        signal from the scheduler). When ``online`` is None (callers that don't
        set it), it falls back to the event-type heuristic (``_MEET_TYPES``) so
        existing flows keep working — and an offline interview gets no Meet link.
        """
        start = self._parse(start_iso)
        end = start + timedelta(minutes=max(duration_min or 30, 1))
        body: dict[str, Any] = {
            "summary": summary,
            "description": description or "",
            "start": {"dateTime": start.isoformat(), "timeZone": _TIME_ZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": _TIME_ZONE},
        }
        if location:
            body["location"] = location
        # Merge + de-duplicate attendee emails (preserve order, drop blanks).
        merged = list(attendees or [])
        if attendee_email:
            merged.append(attendee_email)
        emails = list(dict.fromkeys(e.strip() for e in merged if e and e.strip()))
        if emails:
            body["attendees"] = [{"email": e} for e in emails]
        add_meet = online if online is not None else (event_type in _MEET_TYPES)
        # Attach a Meet on create AND on patch (the latter lets an in-person event
        # be turned online later — e.g. the Physical Interview step). Re-using the
        # same requestId is idempotent: Google returns the existing conference.
        if add_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": request_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        service = self._service(refresh_token)
        events = service.events()
        if google_event_id:
            event = events.patch(
                calendarId=calendar_id,
                eventId=google_event_id,
                body=body,
                conferenceDataVersion=1,
                sendUpdates="all",
            ).execute()
        else:
            event = events.insert(
                calendarId=calendar_id,
                body=body,
                conferenceDataVersion=1 if add_meet else 0,
                sendUpdates="all",
            ).execute()
        logger.info("Pushed calendar event %s (%s)", event.get("id"), event_type)
        return {"id": event.get("id"), "hangoutLink": event.get("hangoutLink")}

    def delete_event(self, refresh_token: str, calendar_id: str, google_event_id: str) -> None:
        service = self._service(refresh_token)
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=google_event_id, sendUpdates="all"
            ).execute()
            logger.info("Deleted calendar event %s", google_event_id)
        except Exception as exc:  # noqa: BLE001
            # 404/410 = already gone; treat as success.
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (404, 410):
                return
            raise

    @staticmethod
    def _parse(start_iso: str) -> datetime:
        """Parse an ISO timestamp; datetime-local strings (no tz) are kept naive
        so Google applies the event timeZone."""
        return datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
