"""Public exit-handover portal endpoints.

An "exit handover" is an unguessable, expiring link HR sends to a departing
employee so they can submit their work-account credentials (encrypted at rest)
and upload handover documents/files. The record itself is created/read by HR
through the generic resources router (`/api/exit-handovers`, keyed by
employeeId); this module adds the token-gated public operations plus an HR-only
reveal/purge for the encrypted credentials.

Token-gated public routes look the record up by its random `token` field (not by
the HR-facing employeeId), so the public link never exposes the employee id.
Expiry is enforced here on the server so an old link can never accept data.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.dependencies import get_repository, get_storage
from app.api.routes.documents import _safe_name
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services.crypto import decrypt_secret, encrypt_secret
from app.storage.base import FileStorage

router = APIRouter(prefix="/api/exit-handovers", tags=["exit-handovers"])

logger = get_logger("curcle.exit_handover")

TABLE = "exit_handovers"
DOCS_TABLE = "documents"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_expired(rec: dict[str, Any]) -> bool:
    expires = _parse_iso(rec.get("expiresAt"))
    return expires is not None and datetime.now(timezone.utc) > expires


def _by_token(repo: DocumentRepository, token: str) -> dict[str, Any]:
    matches = repo.find(TABLE, {"token": token})
    if not matches:
        raise NotFoundError("This handover link is invalid.")
    return matches[0]


def _doc_list(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "documentId": s.get("documentId"),
            "fileName": s.get("fileName"),
            "uploadedAt": s.get("uploadedAt"),
            "size": s.get("size"),
        }
        for s in rec.get("submissions") or []
    ]


# ── Public, token-gated ──────────────────────────────────────────────────────


@router.get("/portal/{token}")
def portal(token: str, repo: DocumentRepository = Depends(get_repository)) -> dict[str, Any]:
    """Public view of a handover link — never returns the stored credentials."""
    rec = _by_token(repo, token)
    return {
        "employeeName": rec.get("employeeName", ""),
        "lastWorkingDay": rec.get("lastWorkingDay", ""),
        "expiresAt": rec.get("expiresAt"),
        "expired": _is_expired(rec),
        "credentialsSubmitted": bool(rec.get("credentials")),
        "documents": _doc_list(rec),
        "status": rec.get("status", ""),
    }


class ExtraCredIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=120)
    value: str = Field(default="", max_length=2048)


class CredentialsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workEmail: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)
    # Optional extra credentials the employee adds as key/value pairs.
    extras: list[ExtraCredIn] = Field(default_factory=list, max_length=50)

    @field_validator("workEmail")
    @classmethod
    def _email(cls, v: str) -> str:
        if "@" not in v or "." not in v:
            raise ValueError("invalid email")
        return v


@router.post("/portal/{token}/credentials")
def submit_credentials(
    token: str,
    body: CredentialsIn,
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Store the employee's work credentials — the password is encrypted at rest."""
    rec = _by_token(repo, token)
    if _is_expired(rec):
        raise ValidationError("This handover link has expired. Please ask HR for a new one.")
    rec["credentials"] = {
        "workEmail": body.workEmail,
        "password": encrypt_secret(settings, body.password),  # encrypted; never store/log plaintext
        # Each extra value is encrypted too — they're credentials/secrets.
        "extras": [
            {"key": e.key, "value": encrypt_secret(settings, e.value)}
            for e in body.extras
            if e.key.strip()
        ],
        "submittedAt": _now(),
    }
    if rec.get("status") in (None, "", "Sent"):
        rec["status"] = "Credentials Submitted"
    rec["updatedAt"] = _now()
    repo.upsert(TABLE, rec["employeeId"], rec)
    logger.info("Exit-handover credentials submitted for handover %s", token)  # no secret in log
    return {"ok": True}


@router.post("/portal/{token}/upload", status_code=201)
def upload(
    token: str,
    file: UploadFile = File(...),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Upload one handover document/file against the link (as many as needed)."""
    rec = _by_token(repo, token)
    if _is_expired(rec):
        raise ValidationError("This handover link has expired. Please ask HR for a new one.")

    # Plain `def`: thread-pooled by Starlette instead of blocking the event
    # loop for the storage/DB calls below. See docs/PERFORMANCE_AUDIT.md P0-1.
    data = file.file.read()
    if not data:
        raise ValidationError("Empty file.")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationError(f"File exceeds the {settings.max_upload_mb} MB limit.")

    employee_id = rec["employeeId"]
    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "file"
    key = f"documents/offboarding/{employee_id}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")

    now = _now()
    # Record in the documents table (entityType offboarding) so HR's existing
    # offboarding documents panel lists it alongside HR-uploaded files.
    repo.upsert(
        DOCS_TABLE,
        doc_id,
        {
            "id": doc_id,
            "entityType": "offboarding",
            "entityId": employee_id,
            "category": "handover",
            "fileName": filename,
            "contentType": file.content_type,
            "size": len(data),
            "storageKey": key,
            "uploadedAt": now,
        },
    )

    submission = {
        "documentId": doc_id,
        "fileName": filename,
        "size": len(data),
        "uploadedAt": now,
    }
    rec["submissions"] = [*(rec.get("submissions") or []), submission]
    rec["updatedAt"] = now
    repo.upsert(TABLE, employee_id, rec)
    logger.info("Exit-handover file uploaded for handover %s.", token)
    return submission


# ── HR-only (open in this app, like the rest of /api/*) ──────────────────────


@router.get("/{employee_id}/reveal")
def reveal(
    employee_id: str,
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return the decrypted credentials + uploaded files for HR."""
    rec = repo.get(TABLE, employee_id)
    if rec is None:
        raise NotFoundError("No handover found for this employee.")
    creds = rec.get("credentials") or {}
    return {
        "workEmail": creds.get("workEmail", ""),
        "password": decrypt_secret(settings, creds.get("password", "")),
        "extras": [
            {"key": e.get("key", ""), "value": decrypt_secret(settings, e.get("value", ""))}
            for e in (creds.get("extras") or [])
        ],
        "submittedAt": creds.get("submittedAt"),
        "documents": _doc_list(rec),
        "status": rec.get("status", ""),
    }


@router.post("/{employee_id}/purge")
def purge(
    employee_id: str,
    repo: DocumentRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Auto-delete the stored credentials once the handover is complete."""
    rec = repo.get(TABLE, employee_id)
    if rec is None:
        raise NotFoundError("No handover found for this employee.")
    rec.pop("credentials", None)
    rec["status"] = "Completed"
    rec["updatedAt"] = _now()
    repo.upsert(TABLE, employee_id, rec)
    logger.info("Exit-handover credentials purged for employee %s.", employee_id)
    return {"ok": True}
