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
from app.services import bgv_checks
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


class VerifyRequest(BaseModel):
    """OfferingCodes HR chose, e.g. ["PANV", "EDUV"]."""

    services: list[str] = []


class VerifyResult(BaseModel):
    ok: bool
    individualId: str | None = None
    checks: list[dict[str, Any]] = []
    reason: str | None = None


def _document_for(
    repo: DocumentRepository,
    storage: FileStorage,
    candidate_id: str,
    doc_type: str,
) -> tuple[bytes, str | None, str] | None:
    """The candidate's uploaded file for a document type, straight from storage.

    Searches every document request for this candidate, not just one: links get
    re-issued and a second request is often created for the documents the first
    one missed, so a candidate's documents are routinely spread across several.
    Prefers a verified submission, then the most recent.
    """
    submissions = [
        s
        for request in repo.list(DOC_REQUESTS)
        if request.get("candidateId") == candidate_id
        for s in request.get("submissions") or []
        if s.get("docType") == doc_type
    ]
    if not submissions:
        return None
    submission = max(
        submissions,
        key=lambda s: (s.get("status") == "Verified", s.get("uploadedAt") or ""),
    )
    meta = repo.get(DOCUMENTS, submission.get("documentId"))
    if not meta or not meta.get("storageKey"):
        return None
    try:
        data, content_type = storage.get(meta["storageKey"])
    except Exception:  # noqa: BLE001 - a storage blip shouldn't fail the batch
        logger.exception("Could not fetch %s from storage.", doc_type)
        return None
    return data, content_type or meta.get("contentType"), meta.get("fileName") or doc_type


@router.post("/{candidate_id}/ongrid-verify", response_model=VerifyResult)
def ongrid_verify(
    candidate_id: str,
    body: VerifyRequest,
    settings: Settings = Depends(get_settings),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> VerifyResult:
    """Actually run the background checks.

    Each check has its own OnGrid endpoint and runs against a document
    registered through `/doc/{slug}/extract` - OnGrid reads the identity number
    off the document itself. Listing codes in a `verifications` array does
    nothing, which is why onboarding alone never started anything.

    Checks are independent: one failing (a missing document, an offering the
    community isn't entitled to) must not stop the rest.
    """
    if not settings.has_ongrid:
        return VerifyResult(ok=False, reason="not_configured")
    if not body.services:
        return VerifyResult(ok=False, reason="no_services")

    if not repo.get(CANDIDATES, candidate_id):
        raise NotFoundError("Candidate not found.")

    bgv = repo.get(BGVS, candidate_id) or {}
    individual_id = str(bgv.get("ongridIndividualId") or "")
    if not individual_id:
        # The individual must exist before any check can reference it.
        return VerifyResult(ok=False, reason="not_onboarded")

    client = OnGridClient(settings)
    # Document ids belong to one OnGrid individual. Re-onboarding creates a new
    # one, and reusing ids across them fails with "Invalid document", so the
    # cache is only good while the individual is unchanged.
    cached_for = str(bgv.get("ongridDocumentIdsFor") or "")
    document_ids: dict[str, str] = (
        dict(bgv.get("ongridDocumentIds") or {}) if cached_for == individual_id else {}
    )

    outcomes: list[dict[str, Any]] = []
    for code in body.services:
        requirement = bgv_checks.CHECK_REQUIREMENTS.get(code.upper())
        document = (
            _document_for(repo, storage, candidate_id, requirement[0]) if requirement else None
        )
        outcome = bgv_checks.run_check(client, individual_id, code, document, document_ids)
        outcomes.append(outcome.as_dict())
        logger.info(
            "OnGrid check %s for candidate %s: %s",
            code,
            candidate_id,
            "started" if outcome.ok else "not started",
        )

    started = [o["code"] for o in outcomes if o["ok"]]
    bgv.setdefault("id", candidate_id)
    bgv.setdefault("candidateId", candidate_id)
    bgv["services"] = body.services
    bgv["ongridDocumentIds"] = document_ids
    bgv["ongridDocumentIdsFor"] = individual_id
    bgv["ongridChecks"] = outcomes
    if started:
        bgv["ongridVerificationsSentAt"] = _now()
        bgv.setdefault("verificationTimeline", []).append({
            "date": _now(),
            "action": f"Started {len(started)} OnGrid check(s): {', '.join(started)}",
            "performedBy": "HR",
        })
    repo.upsert(BGVS, candidate_id, bgv)

    return VerifyResult(ok=bool(started), individualId=individual_id, checks=outcomes)


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
