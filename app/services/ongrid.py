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

# One file part of a multipart upload: (filename, bytes, content type).
UploadFile = tuple[str, bytes, str | None]

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

    def upload_for_extract(
        self,
        individual_id: str,
        slug: str,
        filename: str,
        data: bytes,
        content_type: str | None,
    ) -> dict[str, Any]:
        """Register a document OnGrid can verify against, and let it read the
        document's own number.

        `/doc/{slug}/extract` is what makes a check possible: it returns a
        document id, and OnGrid OCRs the identity number itself (a PAN card
        comes back with `documentUID`). `/doc/other` merely stores a file, which
        is why a check against it fails with "No PAN found to initiate PANV."

        These endpoints reject PDFs ("File Type not supported"), so the caller
        must pass an image.
        """
        body, ctype = self._multipart({}, {"file": (filename, data, content_type)})
        path = f"/v1/individual/{individual_id}/doc/{slug}/extract"
        return self._request("POST", path, body=body, content_type=ctype)

    # -- Records a check is run against ------------------------------------
    # Three offerings verify a *record* rather than a document OnGrid reads for
    # itself. The record carries the claim (this degree, this employer, this
    # address); the check then confirms or refutes it. Each returns an `id` the
    # matching check endpoint takes.

    def add_education_document(
        self, individual_id: str, fields: dict[str, str], file: UploadFile
    ) -> dict[str, Any]:
        """Register one qualification, certificate attached. Returns its `id`.

        Unlike `/doc/{slug}/extract`, OnGrid does not read this document - the
        values it verifies with the institute are the ones we send here, so the
        certificate is evidence rather than the source. Accepts PDFs as well as
        images, so the candidate's file goes up untouched.
        """
        body, ctype = self._multipart(fields, {"file": file})
        path = f"/v1/individual/{individual_id}/doc/edu"
        return self._request("POST", path, body=body, content_type=ctype)

    def add_employment_record(
        self, individual_id: str, fields: dict[str, str], files: dict[str, UploadFile]
    ) -> dict[str, Any]:
        """Register one past employment. Returns its `id`.

        Proof documents are optional - the record stands on its fields alone,
        and OnGrid contacts the employer from the manager/HR details on it.
        """
        body, ctype = self._multipart(fields, files)
        path = f"/v1/individual/{individual_id}/doc/emprecord"
        return self._request("POST", path, body=body, content_type=ctype)

    def add_permanent_address(self, individual_id: str, address: dict[str, Any]) -> dict[str, Any]:
        """Add the individual's permanent address. Returns its `id`.

        Distinct from the current address set at onboarding, which is a plain
        string on a different endpoint. PAV refuses a current address outright
        ("PAV can only be requested against Permanent addresses"), so it has to
        be this one.

        `addOnly` is not optional in practice: without it the endpoint answers
        "addressEntity cannot be null" whatever the body contains.
        """
        body = {"permanentAddress": address, "addOnly": True}
        data = json.dumps(body).encode("utf-8")
        path = f"/v1/individual/{individual_id}/permanentaddress"
        return self._request("POST", path, body=data, content_type="application/json")

    def request_check(
        self, individual_id: str, code: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Start one background check.

        Each offering has its own endpoint, named after the lowercased code -
        `/v1/individual/{id}/panv`, `/empv`, and so on. Sending the codes in a
        `verifications` array does nothing: `/individuals` silently discards
        them and `/individuals/initiate` errors.

        The body says which record to check, and the endpoint names what it
        wants when it is missing ("Document Id can not be null").
        """
        path = f"/v1/individual/{individual_id}/{code.strip().lower()}"
        data = json.dumps(payload).encode("utf-8")
        return self._request("POST", path, body=data, content_type="application/json")

    def _multipart(
        self, fields: dict[str, str], files: dict[str, UploadFile]
    ) -> tuple[bytes, str]:
        """Encode text fields and named file parts as multipart/form-data.

        `files` is keyed by form field name because not every endpoint calls its
        file part "file": an employment record carries `salaryslip`,
        `appointmentletter` and `experienceletter` beside each other, and is
        valid with none of them.
        """
        boundary = f"----CurcleBoundary{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )
        for name, (filename, data, content_type) in files.items():
            ctype = (
                content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
            chunks.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
                ).encode("utf-8")
            )
            chunks.append(data)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
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
            body, ctype = self._multipart({}, {"file": (filename, data, content_type)})
        else:
            path = f"/v1/individual/{individual_id}/doc/other"
            body, ctype = self._multipart(
                {"documentName": doc_type}, {"file": (filename, data, content_type)}
            )
        return self._request("POST", path, body=body, content_type=ctype)
