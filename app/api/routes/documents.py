"""Document storage endpoints (resumes & important files).

Metadata lives in the `documents` Postgres table (via the repository); the blob
lives in AWS S3. Uploads are proxied; downloads are short-lived presigned URLs
so the bucket stays private.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app.api.dependencies import get_repository, get_storage
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services.sessions import COOKIE_NAME, read_session
from app.storage.base import FileStorage


# Categories whose links must open WITHOUT a login, because they are handed to
# people who have no Circle account: resumes (shared with interviewers),
# exit-handover files (the departing employee's portal) and profile/welcome
# photos (rendered as <img>, including in emails). Every other document - ID proofs, PAN,
# Aadhaar, cancelled cheques, offer/appointment letters, BGV reports - needs a
# dashboard session even when the id is known, so a forwarded or leaked link
# exposes nothing sensitive.
PUBLIC_VIEW_CATEGORIES = frozenset({"resume", "handover", "avatar", "Welcome Photo"})


def guard_documents(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Public: the careers-apply resume upload (per-IP rate limited), and viewing
    by id - which `_require_view_access` then narrows to PUBLIC_VIEW_CATEGORIES
    for callers without a session. Listing, deleting and drive imports always
    require a dashboard session."""
    method, path = request.method, request.url.path.rstrip("/")
    if method == "GET" and (path.endswith("/preview") or path.endswith("/url")):
        return
    if method == "POST" and path == "/api/documents":  # resume upload (apply flow)
        return
    if not read_session(settings, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in.")


router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(guard_documents)])

logger = get_logger("curcle.documents")

TABLE = "documents"

_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# Extensions browsers can render inline — used to recover a sensible content type
# when the stored one is missing or a generic octet-stream.
_PREVIEW_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
}

# Google Drive "native" files (Docs/Sheets/Slides) have no binary form and must
# be exported to a real format; everything else downloads byte-for-byte.
_GOOGLE_EXPORT: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", name).strip("_")[:120] or "file"


def _drive_fetch(file_id: str, access_token: str, mime_type: str, limit: int) -> tuple[bytes, str, str]:
    """Download a Drive file with a caller-supplied OAuth token.

    Returns (bytes, content_type, filename_suffix). Native Google files are
    exported; binary files are streamed via alt=media. Raises ValidationError
    on any failure so the client gets a friendly 422 instead of a 500.
    """
    base = "https://www.googleapis.com/drive/v3/files/"
    if mime_type.startswith("application/vnd.google-apps"):
        export_mime, suffix = _GOOGLE_EXPORT.get(mime_type, ("application/pdf", ".pdf"))
        url = f"{base}{urllib.parse.quote(file_id)}/export?mimeType={urllib.parse.quote(export_mime)}"
        content_type = export_mime
    else:
        url = f"{base}{urllib.parse.quote(file_id)}?alt=media&supportsAllDrives=true"
        content_type = mime_type or "application/octet-stream"
        suffix = ""

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed Google host)
            # Read one byte past the limit so we can reject oversize files.
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        logger.warning("Drive download failed for %s: HTTP %s", file_id, exc.code)
        if exc.code in (401, 403):
            raise ValidationError("Google Drive access was denied or expired. Reconnect and try again.") from exc
        raise ValidationError("Could not fetch the file from Google Drive.") from exc
    except urllib.error.URLError as exc:
        logger.warning("Drive download error for %s: %s", file_id, exc)
        raise ValidationError("Could not reach Google Drive. Check your connection and try again.") from exc

    if not data:
        raise ValidationError("The selected Google Drive file is empty.")
    if len(data) > limit:
        raise ValidationError("The selected file exceeds the upload size limit.")
    return data, content_type, suffix


class DriveImport(BaseModel):
    entityType: str
    entityId: str
    category: str = "document"
    fileId: str
    fileName: str = "file"
    mimeType: str = ""
    accessToken: str = Field(..., min_length=1)


def _get_or_404(repo: DocumentRepository, doc_id: str) -> dict[str, Any]:
    doc = repo.get(TABLE, doc_id)
    if doc is None:
        raise NotFoundError(f"document '{doc_id}' not found")
    return doc


def _require_view_access(request: Request, settings: Settings, doc: dict[str, Any]) -> None:
    """Anyone may view a public-category document by id; everything else needs a
    dashboard session."""
    if doc.get("category") in PUBLIC_VIEW_CATEGORIES:
        return
    if not read_session(settings, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in.")


@router.get("")
def list_documents(
    entityType: str | None = None,
    entityId: str | None = None,
    repo: DocumentRepository = Depends(get_repository),
) -> list[dict[str, Any]]:
    match: dict[str, Any] = {}
    if entityType:
        match["entityType"] = entityType
    if entityId:
        match["entityId"] = entityId
    # Indexed JSONB containment (GIN) instead of loading the whole table.
    items = repo.find(TABLE, match) if match else repo.list(TABLE)
    return sorted(items, key=lambda d: d.get("uploadedAt", ""), reverse=True)


@router.post("", status_code=201)
async def upload_document(
    entityType: str = Form(...),
    entityId: str = Form(...),
    category: str = Form("document"),
    file: UploadFile = File(...),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise ValidationError("Empty file.")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationError(f"File exceeds the {settings.max_upload_mb} MB limit.")

    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "file"
    key = f"documents/{entityType}/{entityId}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")

    meta = {
        "id": doc_id,
        "entityType": entityType,
        "entityId": entityId,
        "category": category,
        "fileName": filename,
        "contentType": file.content_type,
        "size": len(data),
        "storageKey": key,
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }
    repo.upsert(TABLE, doc_id, meta)
    return meta


@router.post("/from-drive", status_code=201)
def import_from_drive(
    body: DriveImport,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Import a copy of a Google Drive file into S3.

    The browser supplies a short-lived OAuth access token (drive.readonly) from
    the Google Picker; the backend pulls the bytes once and stores them like any
    other upload, so the file becomes self-contained (no Drive dependency after).
    """
    limit = settings.max_upload_mb * 1024 * 1024
    data, content_type, suffix = _drive_fetch(body.fileId, body.accessToken, body.mimeType, limit)

    filename = body.fileName or "file"
    if suffix and not filename.lower().endswith(suffix):
        filename = f"{filename}{suffix}"

    doc_id = uuid.uuid4().hex[:12]
    key = f"documents/{body.entityType}/{body.entityId}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, content_type)

    meta = {
        "id": doc_id,
        "entityType": body.entityType,
        "entityId": body.entityId,
        "category": body.category,
        "fileName": filename,
        "contentType": content_type,
        "size": len(data),
        "storageKey": key,
        "source": "google-drive",
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }
    repo.upsert(TABLE, doc_id, meta)
    return meta


@router.get("/{doc_id}/url")
def get_download_url(
    doc_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> dict[str, Any]:
    doc = _get_or_404(repo, doc_id)
    _require_view_access(request, settings, doc)
    # Serve the file inline so previewable types (PDF, images) open in the browser
    # tab instead of downloading; non-previewable types still resolve fine.
    filename = _safe_name(doc.get("fileName") or "file")
    url = storage.presigned_url(
        doc["storageKey"],
        disposition=f'inline; filename="{filename}"',
        content_type=doc.get("contentType") or None,
    )
    return {"url": url, "fileName": doc.get("fileName")}


@router.get("/{doc_id}/preview")
def preview_document(
    doc_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> Response:
    """Stream a document inline so it opens in the browser tab (no download).

    Unlike the presigned `/url` route, this proxies the bytes through the API and
    sets `Content-Disposition: inline` ourselves — reliable regardless of whether
    the object store honours response-header overrides.
    """
    doc = _get_or_404(repo, doc_id)
    _require_view_access(request, settings, doc)
    data, stored_type = storage.get(doc["storageKey"])
    filename = _safe_name(doc.get("fileName") or "file")

    content_type = doc.get("contentType") or stored_type or "application/octet-stream"
    # Recover a previewable type from the extension if the stored one is generic.
    if content_type in ("", "application/octet-stream"):
        ext = os.path.splitext(filename.lower())[1]
        content_type = _PREVIEW_MIME.get(ext, content_type)

    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.delete("/{doc_id}", status_code=204, response_class=Response)
def delete_document(
    doc_id: str,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> Response:
    doc = _get_or_404(repo, doc_id)
    storage.delete(doc["storageKey"])
    repo.delete(TABLE, doc_id)
    return Response(status_code=204)
