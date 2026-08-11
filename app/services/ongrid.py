"""OnGrid background-verification client.

Onboards a hired candidate into an OnGrid community and pushes their document
images. It does **not** trigger verifications — that is done by HR inside the
OnGrid portal. Sending an empty (absent) `verifications` list makes the create
call save the individual without starting any check (verified against OnGrid
staging).

Pure transport + payload shaping, no FastAPI imports — mirrors
`email_sender._deliver_resend` (stdlib `urllib`, Basic auth, short timeout,
normal User-Agent to dodge Cloudflare 1010). Methods raise `OnGridError` on
failure; callers decide how to surface it.

Reference (verified against live staging this session, since the published spec
is stale):
  - POST {base}/v1/community/{communityId}/individuals            → onboard only
  - POST {base}/v1/community/{communityId}/individuals/initiate   → onboard (+checks)
    Both accept the same identity body; with no `verifications`, `initiate`
    behaves as onboard-only. We try the plain path first and fall back.
  - POST {base}/v1/individual/{individualId}/doc/{slug}           → attach a file
    Only these slugs are reachable: pan, vid, dl, passport, edu, loa, other.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("curcle.ongrid")

_TIMEOUT = 30
_HTTP_USER_AGENT = "Mozilla/5.0 (compatible; CurcleBackend/1.0; +https://optiminastic.com)"

# How each of our joining-document types is attached to an OnGrid individual.
# Verified live: two file-only endpoints exist that also OCR the card on our
# side (`/doc/pan/extract`, `/doc/vid/extract`) — but those reject anything
# that isn't already an image (a PDF 400s with "File Type not supported"), and
# OnGrid runs its own OCR later regardless of which endpoint received the
# file. So every document, PAN/Voter ID included, goes through the plain
# `/doc/other` route below — whatever format the candidate uploaded, as-is.
#   value = ("extract", slug)  -> POST /doc/{slug}/extract, field: file
#   value = ("other", name)    -> POST /doc/other, fields: file + documentName
DOC_TYPE_ROUTING: dict[str, tuple[str, str]] = {}

# Our stored gender label → OnGrid's single-letter code (M/F/T/O/U).
GENDER_TO_ONGRID: dict[str, str] = {"Male": "M", "Female": "F", "Other": "O"}


class OnGridError(RuntimeError):
    """An OnGrid API call failed. `.status` is the HTTP code when known."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OnGridClient:
    """Thin OnGrid API wrapper. One instance per request; takes Settings only."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.ongrid_base_url.rstrip("/")
        self._community_id = settings.ongrid_community_id
        token = f"{settings.ongrid_username}:{settings.ongrid_password}".encode()
        self._auth = "Basic " + base64.b64encode(token).decode()

    # -- HTTP helpers ------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": self._auth,
            "Accept": "application/json",
            "User-Agent": _HTTP_USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(
            f"{self._base}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (fixed host)
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OnGridError(f"OnGrid {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise OnGridError(f"Could not reach OnGrid: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    # -- API ---------------------------------------------------------------

    def create_individual(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Onboard (save) an individual in the community — no verifications.

        `payload` must already carry OnGrid's field names (name, professionId,
        city, gender, phone, hasConsent, consentText, ...). We strip any
        `verifications` key so this can never accidentally start a check.
        """
        body = {k: v for k, v in payload.items() if k != "verifications"}
        data = json.dumps(body).encode("utf-8")
        base = f"/v1/community/{self._community_id}/individuals"
        try:
            return self._request("POST", base, body=data, content_type="application/json")
        except OnGridError as exc:
            # The plain onboard path is absent from the stale spec; if this
            # deployment doesn't expose it, fall back to /initiate with no
            # verifications (proven equivalent: onboard-only).
            if exc.status == 404:
                logger.info("OnGrid /individuals 404 — falling back to /initiate")
                return self._request(
                    "POST", f"{base}/initiate", body=data, content_type="application/json"
                )
            raise

    def _multipart(
        self, filename: str, data: bytes, content_type: str | None, fields: dict[str, str]
    ) -> tuple[bytes, str]:
        """Encode `fields` + one `file` part as multipart/form-data."""
        boundary = f"----CurcleBoundary{uuid.uuid4().hex}"
        ctype = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
            ).encode("utf-8")
        )
        chunks.append(data)
        chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def upload_document(
        self,
        individual_id: str,
        doc_type: str,
        filename: str,
        data: bytes,
        content_type: str | None,
    ) -> dict[str, Any]:
        """Attach one document image to an individual, routed by doc type.

        PAN/Voter go to their `/extract` endpoint (file only); everything else is
        added via `/doc/other` with the document type as `documentName`.
        """
        mode, slug = DOC_TYPE_ROUTING.get(doc_type, ("other", doc_type))
        if mode == "extract":
            path = f"/v1/individual/{individual_id}/doc/{slug}/extract"
            body, ctype = self._multipart(filename, data, content_type, {})
        else:
            path = f"/v1/individual/{individual_id}/doc/other"
            body, ctype = self._multipart(
                filename, data, content_type, {"documentName": doc_type}
            )
        return self._request("POST", path, body=body, content_type=ctype)


def slug_for_doc_type(doc_type: str) -> str:
    """OnGrid /doc slug for one of our joining-document types."""
    return DOC_TYPE_TO_SLUG.get(doc_type, _FALLBACK_SLUG)
