"""OnGrid onboarding endpoint (HR-triggered).

`POST /api/bgv/{candidate_id}/ongrid-onboard` pushes a hired candidate into the
OnGrid community: it creates the individual from the identity we hold, then
uploads each accepted joining-document image. It does **not** start any
verification — HR triggers those in OnGrid's own portal.

Session-guarded (an HR action). Runs synchronously so the UI can show OnGrid's
real response; the individual-create and file uploads are a few sequential HTTP
calls. Failures return a structured result rather than a 500 so partial success
(individual created, one file failed) is visible to HR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_repository, get_storage, require_user
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services.ongrid import GENDER_TO_ONGRID, OnGridClient, OnGridError
from app.storage.base import FileStorage

router = APIRouter(prefix="/api/bgv", tags=["bgv"], dependencies=[Depends(require_user)])

logger = get_logger("curcle.bgv_ongrid")

CANDIDATES = "candidates"
DOC_REQUESTS = "doc_requests"
DOCUMENTS = "documents"
BGVS = "bgvs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _phone_digits(raw: str) -> str:
    """OnGrid wants the bare 10-digit mobile; our records store '+91 9876543210'."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _pick_doc_request(repo: DocumentRepository, candidate_id: str) -> dict[str, Any] | None:
    """The candidate's joining-docs request with the most content (resends mint
    new links, so several can exist; the fullest one is the real submission)."""
    requests = [
        r
        for r in repo.list(DOC_REQUESTS)
        if r.get("candidateId") == candidate_id
        and r.get("kind") not in ("signed-offer", "signed-appointment")
    ]
    if not requests:
        return None
    return max(
        requests,
        key=lambda r: len(r.get("submissions") or []) + (1 if r.get("bankDetails") else 0),
    )


class OnboardResult(BaseModel):
    ok: bool
    individualId: str | None = None
    documents: list[dict[str, Any]] = []
    response: dict[str, Any] | None = None
    reason: str | None = None


@router.post("/{candidate_id}/ongrid-onboard", response_model=OnboardResult)
def ongrid_onboard(
    candidate_id: str,
    settings: Settings = Depends(get_settings),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> OnboardResult:
    if not settings.has_ongrid:
        return OnboardResult(ok=False, reason="not_configured")

    candidate = repo.get(CANDIDATES, candidate_id)
    if not candidate:
        raise NotFoundError("Candidate not found.")

    doc_request = _pick_doc_request(repo, candidate_id)
    consent = (doc_request or {}).get("consent") or {}
    if not consent.get("agreed") or not str(consent.get("text") or "").strip():
        # OnGrid mandates consent (error 157 otherwise); we require the
        # candidate's recorded portal consent before sending any PII.
        return OnboardResult(ok=False, reason="no_consent")

    gender = GENDER_TO_ONGRID.get(str(candidate.get("gender") or ""))
    if not gender:
        return OnboardResult(ok=False, reason="no_gender")

    city = str(candidate.get("location") or "").strip() or "NA"
    payload: dict[str, Any] = {
        "name": candidate.get("fullName") or "",
        "professionId": "1",
        "city": city,
        "gender": gender,
        "phone": _phone_digits(candidate.get("phone") or ""),
        "hasConsent": True,
        "consentText": consent["text"],
    }
    if candidate.get("email"):
        payload["email"] = candidate["email"]

    client = OnGridClient(settings)

    # 1) Create (onboard-only) the individual.
    try:
        created = client.create_individual(payload)
    except OnGridError as exc:
        logger.warning("OnGrid create failed for candidate %s: %s", candidate_id, exc)
        return OnboardResult(ok=False, reason=str(exc))

    individual = created.get("individual") or created
    individual_id = str(individual.get("id") or "")
    if not individual_id:
        return OnboardResult(ok=False, reason="OnGrid did not return an individual id.")

    # 2) Upload each accepted document image.
    doc_results: list[dict[str, Any]] = []
    for sub in (doc_request or {}).get("submissions") or []:
        doc_type = sub.get("docType") or ""
        # Only push documents HR has cleared.
        if sub.get("status") not in ("Verified", "Submitted"):
            continue
        document_id = sub.get("documentId")
        meta = repo.get(DOCUMENTS, document_id) if document_id else None
        if not meta:
            doc_results.append({"docType": doc_type, "status": "missing"})
            continue
        # Every document type is attached as-is via /doc/other — OnGrid runs
        # its own OCR later, so we don't route any type through /extract.
        route = "other"
        try:
            data, content_type = storage.get(meta["storageKey"])
            client.upload_document(
                individual_id,
                doc_type,
                meta.get("fileName") or "document",
                data,
                content_type or meta.get("contentType"),
            )
            doc_results.append({"docType": doc_type, "route": route, "status": "uploaded"})
        except OnGridError as exc:
            logger.warning("OnGrid doc upload failed (%s): %s", doc_type, exc)
            doc_results.append({"docType": doc_type, "route": route, "status": "failed"})
        except Exception as exc:  # noqa: BLE001 — report per-file, never 500 the batch
            logger.warning("Reading/uploading doc failed (%s): %s", doc_type, exc)
            doc_results.append({"docType": doc_type, "route": route, "status": "failed"})

    # 3) Persist onto the BGV record (keyed by candidateId).
    trimmed = {
        "id": individual_id,
        "name": individual.get("name"),
        "city": individual.get("city"),
        "phone": individual.get("phone"),
        "gender": individual.get("gender"),
        "currentAddress": individual.get("currentAddress"),
    }
    bgv = repo.get(BGVS, candidate_id) or {
        "id": candidate_id,
        "candidateId": candidate_id,
        "candidateName": candidate.get("fullName"),
        "appliedRole": candidate.get("appliedRole"),
        "documents": [],
        "overallStatus": "Pending",
        "verificationTimeline": [],
    }
    bgv["ongridIndividualId"] = individual_id
    bgv["ongridOnboardedAt"] = _now()
    bgv["ongridResponse"] = trimmed
    bgv["ongridDocuments"] = doc_results
    timeline = list(bgv.get("verificationTimeline") or [])
    uploaded = sum(1 for d in doc_results if d["status"] == "uploaded")
    timeline.append(
        {
            "date": _now(),
            "action": f"Onboarded to OnGrid (individual {individual_id}); "
            f"{uploaded}/{len(doc_results)} documents uploaded",
            "performedBy": "HR",
        }
    )
    bgv["verificationTimeline"] = timeline
    repo.upsert(BGVS, candidate_id, bgv)

    return OnboardResult(
        ok=True, individualId=individual_id, documents=doc_results, response=trimmed
    )
