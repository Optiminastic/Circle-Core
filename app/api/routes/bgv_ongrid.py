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
from app.api.routes.doc_requests import has_consent
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
    #: True when this landed on the candidate's existing OnGrid individual
    #: instead of creating one, so HR can see a retry did not duplicate them.
    reused: bool = False


class ClaimDetails(BaseModel):
    """What HR entered for the checks that verify a stated claim.

    The dialog pre-fills these from the candidate's own portal answers, so what
    arrives here is usually theirs, reviewed. Anything present wins over the
    stored copy: HR has just looked at it, and they are the ones accountable
    for what gets sent.
    """

    uan: str | None = None
    education: dict[str, Any] | None = None
    employment: dict[str, Any] | None = None
    permanentAddress: dict[str, Any] | None = None


class VerifyRequest(BaseModel):
    """OfferingCodes HR chose, e.g. ["PANV", "EDUV"], and what they typed."""

    services: list[str] = []
    details: ClaimDetails | None = None


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


def _portal_field(repo: DocumentRepository, candidate_id: str, key: str) -> Any:
    """One value the candidate entered, from whichever of their document
    requests holds it. Links get re-issued, so their answers end up spread
    across several records and only one of them will have any given field."""
    return next(
        (
            request[key]
            for request in repo.list(DOC_REQUESTS)
            if request.get("candidateId") == candidate_id and request.get(key)
        ),
        None,
    )


def _candidate_data(
    repo: DocumentRepository,
    storage: FileStorage,
    candidate_id: str,
    settings: Settings,
    entered: ClaimDetails | None = None,
) -> bgv_checks.CandidateData:
    """Everything the candidate supplied that a check might verify.

    Gathered once per run rather than per check, so two checks wanting the same
    file don't fetch it twice. Each field is independently optional - a check
    reports for itself what it is missing.
    """
    proofs = {
        field_name: file
        for doc_type, field_name in bgv_checks.EMPLOYMENT_PROOF_DOCS.items()
        if (file := _document_for(repo, storage, candidate_id, doc_type)) is not None
    }

    def claim(key: str) -> Any:
        """What HR entered, falling back to what the candidate saved."""
        typed = getattr(entered, key, None) if entered else None
        return typed or _portal_field(repo, candidate_id, key)

    uan = claim("uan")
    return bgv_checks.CandidateData(
        uan=str(uan).strip() if uan else None,
        education=claim("education"),
        employment=claim("employment"),
        permanent_address=claim("permanentAddress"),
        references=list(_portal_field(repo, candidate_id, "references") or []),
        is_fresher=bool(_portal_field(repo, candidate_id, "isFresher")),
        education_file=_document_for(
            repo, storage, candidate_id, bgv_checks.EDUCATION_DOC_TYPE
        ),
        employment_files=proofs,
        prc_schema_id=settings.ongrid_prc_schema_id,
    )


#: A record carrying no community predates the stamp. It cannot be shown to
#: belong here, and an id from somewhere else is not merely stale - OnGrid may
#: well have an individual under that number, and it will be a different person.
#: So an unstamped record is treated as foreign rather than assumed to be ours.
FOREIGN_COMMUNITY = "different_ongrid_community"


def belongs_here(bgv: dict[str, Any], settings: Settings) -> bool:
    """Was this individual created in the community we are configured for?

    Individual ids are per community, so the same number means different people
    in staging and in production. Reading one across the boundary does not fail
    loudly - it quietly answers about somebody else.
    """
    return str(bgv.get("ongridCommunityId") or "") == str(settings.ongrid_community_id or "")


class StatusResult(BaseModel):
    ok: bool
    individualId: str | None = None
    overallStatus: str | None = None
    """One row per check OnGrid is actually running, with its own state."""
    checks: list[dict[str, str]] = []
    reportUrl: str | None = None
    reason: str | None = None


@router.get("/{candidate_id}/ongrid-status", response_model=StatusResult)
def ongrid_status(
    candidate_id: str,
    settings: Settings = Depends(get_settings),
    repo: DocumentRepository = Depends(get_repository),
) -> StatusResult:
    """What OnGrid is actually running for this candidate, and the report if ready.

    Deliberately read from OnGrid rather than from our own record. The two are
    not the same: `services` says what HR asked for, and checks selected before
    the per-offering endpoints existed were saved there but never started, so
    the stored list has claimed verifications that do not exist.
    """
    if not settings.has_ongrid:
        return StatusResult(ok=False, reason="not_configured")

    bgv = repo.get(BGVS, candidate_id) or {}
    individual_id = str(bgv.get("ongridIndividualId") or "")
    if not individual_id:
        return StatusResult(ok=False, reason="not_onboarded")
    if not belongs_here(bgv, settings):
        # Asking anyway would return whoever holds that id in this community.
        logger.info("OnGrid status skipped for candidate %s: foreign community", candidate_id)
        return StatusResult(ok=False, individualId=individual_id, reason=FOREIGN_COMMUNITY)

    client = OnGridClient(settings)
    try:
        payload = client.verification_status(individual_id)
    except OnGridError as exc:
        logger.warning("OnGrid status failed for candidate %s: %s", candidate_id, exc)
        return StatusResult(ok=False, individualId=individual_id, reason=str(exc))

    overall, checks = bgv_checks.parse_status(payload)

    # Best-effort: the report only exists once every check has finished, and
    # asking early is an error rather than an empty answer. A missing report
    # must not turn a perfectly good status into a failure.
    report = payload.get("consolidatedReportUrl")
    if not report and checks:
        try:
            report = client.consolidated_report(individual_id).get("servingUrl")
        except OnGridError:
            report = None

    # Cached so the panel has something to show before this call returns, and
    # so the record keeps the last known state if OnGrid is unreachable.
    bgv["ongridStatus"] = {"overall": overall, "checks": checks, "reportUrl": report}
    bgv["ongridStatusAt"] = _now()
    repo.upsert(BGVS, candidate_id, bgv)

    return StatusResult(
        ok=True,
        individualId=individual_id,
        overallStatus=overall,
        checks=checks,
        reportUrl=str(report) if report else None,
    )


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
    if not belongs_here(bgv, settings):
        # Worse than a stale read: this would start, and bill, real checks
        # against whoever holds that id in the community we are pointed at.
        logger.warning("OnGrid verify refused for candidate %s: foreign community", candidate_id)
        return VerifyResult(ok=False, reason=FOREIGN_COMMUNITY)

    data = _candidate_data(repo, storage, candidate_id, settings, body.details)

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
        outcome = bgv_checks.run_check(
            client, individual_id, code, document, document_ids, data
        )
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
    if body.details:
        # Kept so a re-run pre-fills with what was actually sent, and so the
        # record shows what a check was run against rather than only its result.
        bgv["claimDetails"] = body.details.model_dump(exclude_none=True)
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
    if not has_consent(doc_request or {}):
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

    bgv = repo.get(BGVS, candidate_id) or {
        "id": candidate_id,
        "candidateId": candidate_id,
        "candidateName": candidate.get("fullName"),
        "appliedRole": candidate.get("appliedRole"),
        "documents": [],
        "overallStatus": "Pending",
        "verificationTimeline": [],
    }

    # 1) Create (onboard-only) the individual — unless this candidate already
    #    has one. A second create does not update the first, it forks it:
    #    OnGrid mints a new id, we would overwrite ours, and any check already
    #    running against the original goes invisible here while OnGrid keeps
    #    billing for it. HR retries this whenever a document failed to upload,
    #    which is precisely when the retry has to land on the same individual.
    existing_id = str(bgv.get("ongridIndividualId") or "")
    reused = bool(existing_id)
    if reused:
        individual_id = existing_id
        individual = dict(bgv.get("ongridResponse") or {"id": existing_id})
        logger.info("OnGrid re-onboard for candidate %s reusing individual", candidate_id)
    else:
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
    bgv["ongridIndividualId"] = individual_id
    # Which OnGrid this id means something in. Without it, a change of
    # environment turns every stored id into a pointer at a stranger.
    bgv["ongridCommunityId"] = str(settings.ongrid_community_id or "")
    bgv["ongridBaseUrl"] = settings.ongrid_base_url
    bgv["ongridOnboardedAt"] = _now()
    bgv["ongridResponse"] = trimmed
    bgv["ongridDocuments"] = doc_results
    timeline = list(bgv.get("verificationTimeline") or [])
    uploaded = sum(1 for d in doc_results if d["status"] == "uploaded")
    opened = (
        f"Re-sent documents to OnGrid (existing individual {individual_id})"
        if reused
        else f"Onboarded to OnGrid (individual {individual_id})"
    )
    timeline.append(
        {
            "date": _now(),
            "action": f"{opened}; {uploaded}/{len(doc_results)} documents uploaded",
            "performedBy": "HR",
        }
    )
    bgv["verificationTimeline"] = timeline
    repo.upsert(BGVS, candidate_id, bgv)

    return OnboardResult(
        ok=True,
        individualId=individual_id,
        documents=doc_results,
        response=trimmed,
        reused=reused,
    )
