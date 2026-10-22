"""What Circle hands to Avora about one employee: pay, bank, documents, personal
and employment details, appraisal history and assets.

This is the ONLY path by which compensation or documents leave Circle, and it
is deliberately separate from the directory export / id-sync push (which carry
directory fields only and feed many apps). It is authenticated with its own
secret (AVORA_API_SECRET), so holding the directory secret grants nothing here.

Circle reports what it has, as it has it; Avora validates and decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Not employee paperwork: profile pictures are shown in Circle's own UI only.
_EXCLUDED_DOCUMENT_CATEGORIES = frozenset({"avatar", "Welcome Photo"})

_LAKH = 100_000
_LAKH_PATTERN = re.compile(r"lpa|lakh|\bl\b", re.IGNORECASE)
_NON_NUMERIC = re.compile(r"[^0-9.]")


def parse_annual_ctc(value: Any) -> int | None:
    """Free-text CTC ("12 LPA", "1,80,000", "180000") to annual rupees.

    Mirrors the frontend's `parseAnnualCtc` (fe/lib/ctc.ts) so both read a
    value the same way.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        number = float(_NON_NUMERIC.sub("", value))
    except ValueError:
        return None
    if number <= 0:
        return None
    if _LAKH_PATTERN.search(value):
        return round(number * _LAKH)
    return round(number)


def _pf_enabled(breakdown: Any) -> bool | None:
    """True/False when Circle's breakdown says whether PF applies; None when
    there is no breakdown to read it from."""
    if not isinstance(breakdown, dict):
        return None
    pf = (breakdown.get("employerPf") or 0, breakdown.get("employeePf") or 0)
    try:
        return any(float(v) > 0 for v in pf)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _offer_ctc(offer: dict[str, Any]) -> int | None:
    value = offer.get("ctcAnnual")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return round(value)


def compensation_for(
    employee: dict[str, Any], offer_letter: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The employee record is the current truth (it moves with appraisals);
    the offer letter HR built at hiring fills whatever the record lacks. Its
    figures are exact numbers, unlike the record's free-text CTC."""
    details = employee.get("personalDetails") or {}
    offer = offer_letter or {}
    annual = parse_annual_ctc(employee.get("annualCtc"))
    ctc_text = _text(employee.get("annualCtc"))
    if annual is None and _offer_ctc(offer) is not None:
        annual = _offer_ctc(offer)
        ctc_text = f"{annual} (from offer letter)"
    pf_enabled = _pf_enabled(employee.get("ctcBreakdown"))
    if pf_enabled is None and isinstance(offer.get("pfEnabled"), bool):
        pf_enabled = offer["pfEnabled"]
    return {
        "employee_code": employee.get("id"),
        "annual_ctc_text": ctc_text,
        "annual_ctc_inr": annual,
        "pf_enabled": pf_enabled,
        "joining_date": _text(employee.get("joiningDate")) or _text(offer.get("joiningDate")),
        "bank": {
            "bank_name": _text(details.get("bankName")),
            "account_number": _text(details.get("accountNumber")),
            "ifsc_code": _text(details.get("ifsc")),
        },
    }


def document_entry(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id"),
        "file_name": doc.get("fileName"),
        "category": doc.get("category"),
        "content_type": doc.get("contentType"),
        "size": doc.get("size"),
        "uploaded_at": doc.get("uploadedAt"),
    }


def owner_refs(employee: dict[str, Any]) -> set[tuple[str, str]]:
    """Every (entityType, entityId) this person's files can sit under: their
    employee record, and the candidate record they were hired from. Joining
    documents uploaded before conversion that were never moved - or after it,
    through a still-open documents link - stay on the candidate."""
    refs = {("employee", str(employee.get("id") or ""))}
    if employee.get("candidateId"):
        refs.add(("candidate", str(employee["candidateId"])))
    return {ref for ref in refs if ref[1]}


# Letters the candidate signs and returns: accepted unless HR rejects the copy
# (the onboarding stepper's own rule). Every other requested document - ID
# proofs, certificates, bank proof - counts only once HR has verified it.
_SIGNED_LETTERS = frozenset(
    {"Signed Offer Letter", "Signed Appointment Letter", "Signed Internship Letter"}
)


def _accepted(submission: dict[str, Any]) -> bool:
    status = submission.get("status")
    if submission.get("docType") in _SIGNED_LETTERS:
        return status != "Rejected"
    return status == "Verified"


@dataclass(frozen=True)
class RequestReview:
    """What HR decided about this person's requested uploads.

    `accepted_ids`: the current upload of each requested document type, when
    accepted. `requested_types`: every type ever requested - any OTHER file in
    one of these categories is a superseded, rejected or unreviewed upload.
    """

    accepted_ids: frozenset[str]
    requested_types: frozenset[str]


def review_of(requests: list[dict[str, Any]]) -> RequestReview:
    """Latest submission per document type across all of the person's requests
    (re-sent links mint new requests; the newest upload is the live one)."""
    latest: dict[str, dict[str, Any]] = {}
    types: set[str] = set()
    for request in requests:
        types.update(str(t) for t in request.get("requiredDocs") or [])
        for sub in request.get("submissions") or []:
            doc_type = str(sub.get("docType") or "")
            if not doc_type:
                continue
            types.add(doc_type)
            current = latest.get(doc_type)
            if current is None or str(sub.get("uploadedAt") or "") >= str(current.get("uploadedAt") or ""):
                latest[doc_type] = sub
    accepted = {str(s.get("documentId")) for s in latest.values() if _accepted(s) and s.get("documentId")}
    return RequestReview(frozenset(accepted), frozenset(types))


def is_shareable_document(
    doc: dict[str, Any], refs: set[tuple[str, str]], review: RequestReview
) -> bool:
    """Only this person's own, current, accepted paperwork: never another
    entity's file, never a profile photo, and never an upload HR rejected, has
    not reviewed yet, or that a newer upload replaced."""
    if (doc.get("entityType"), doc.get("entityId")) not in refs:
        return False
    if doc.get("category") in _EXCLUDED_DOCUMENT_CATEGORIES or not doc.get("storageKey"):
        return False
    if str(doc.get("id")) in review.accepted_ids:
        return True
    # Files HR attached directly (not through a document request) are shared.
    return doc.get("category") not in review.requested_types


_AVATAR_URL_ID = re.compile(r"/api/documents/([A-Za-z0-9_-]{1,64})/preview")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def avatar_document_id(employee: dict[str, Any]) -> str | None:
    """The document behind the employee's profile photo (`avatarUrl` is that
    document's preview link)."""
    found = _AVATAR_URL_ID.search(str(employee.get("avatarUrl") or ""))
    return found.group(1) if found else None


def buddy_code(onboarding: dict[str, Any] | None) -> str | None:
    """The employee code of the onboarding buddy HR assigned at step 6."""
    return _text((onboarding or {}).get("buddyEmployeeId"))


def colleague_email(colleague: dict[str, Any] | None) -> str | None:
    """A colleague's work email, the key Avora matches people on. Someone who
    has since left is nobody's buddy or manager any more."""
    if not colleague or colleague.get("status") == "Offboarded":
        return None
    email = _text(colleague.get("email"))
    return email.lower() if email and "@" in email else None


def buddy_email(buddy: dict[str, Any] | None) -> str | None:
    return colleague_email(buddy)


def manager_code(employee: dict[str, Any]) -> str | None:
    """The reporting manager's employee code, set by Circle's manager picker
    (older records only carry a typed name, which Avora cannot match)."""
    return _text(employee.get("reportingManagerId"))


# --- ID numbers: only ever leave Circle masked -----------------------------------

_ID_VISIBLE = 4
# Shorter than this is not a real PAN/Aadhaar/UAN, and masking it would still
# reveal most of it - send nothing instead.
_ID_MIN_LENGTH = 8
_AADHAAR_LENGTH = 12
_NOT_ALNUM = re.compile(r"[^0-9A-Za-z]")


def mask_id_number(value: Any, *, grouped: bool = False) -> str | None:
    """Everything but the last 4 characters as X ("ABCDE1234F" -> "XXXXXX234F").

    `grouped` writes a 12-digit Aadhaar the way the card does
    ("XXXX XXXX 1234"). A value already masked here (or by OCR) masks to the
    same thing, so re-sending is harmless; one with nothing readable left in
    its last 4 characters is not sent at all.
    """
    cleaned = _NOT_ALNUM.sub("", str(value or "")).upper()
    tail = cleaned[-_ID_VISIBLE:]
    if len(cleaned) < _ID_MIN_LENGTH or set(tail) == {"X"}:
        return None
    if grouped and len(cleaned) == _AADHAAR_LENGTH:
        return f"XXXX XXXX {tail}"
    return "X" * (len(cleaned) - _ID_VISIBLE) + tail


def latest_uan(requests: list[dict[str, Any]]) -> str | None:
    """The UAN from the newest document request that carries one."""
    with_uan = [r for r in requests if _text(r.get("uan"))]
    newest = max(
        with_uan,
        key=lambda r: str(r.get("updatedAt") or r.get("createdAt") or ""),
        default=None,
    )
    return _text(newest.get("uan")) if newest else None


def profile_for(
    employee: dict[str, Any],
    buddy: dict[str, Any] | None = None,
    *,
    manager: dict[str, Any] | None = None,
    uan: str | None = None,
) -> dict[str, Any]:
    """Personal and employment details Avora mirrors (Circle owns them), plus
    the onboarding buddy and reporting manager. Never sent through id-sync.

    ID numbers are masked HERE, so the full PAN, Aadhaar and UAN never leave
    Circle."""
    details = employee.get("personalDetails") or {}
    dob = _text(details.get("dateOfBirth"))
    return {
        "employee_code": employee.get("id"),
        "date_of_birth": dob if dob and _ISO_DAY.match(dob) else None,
        "gender": _text(details.get("gender")),
        "avatar_document_id": avatar_document_id(employee),
        "buddy_email": buddy_email(buddy),
        "manager_code": manager_code(employee),
        "manager_email": colleague_email(manager),
        "phone": _text(employee.get("phone")),
        "personal_email": _text(employee.get("personalEmail")),
        "address": _text(details.get("address")),
        "emergency_contact": _text(details.get("emergencyContact")),
        "employment_type": _text(employee.get("employmentType")),
        "work_location": _text(employee.get("workLocation")),
        "pan_masked": mask_id_number(details.get("panNumber")),
        "aadhaar_masked": mask_id_number(details.get("aadhaarNumber"), grouped=True),
        "uan_masked": mask_id_number(uan),
    }


# --- Appraisal history and assets ----------------------------------------------


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def appraisal_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(record.get("id")),
        "period": _text(record.get("reviewPeriod")),
        "status": _text(record.get("status")),
        "current_salary": _text(record.get("currentSalary")),
        "revised_salary": _text(record.get("recommendedSalaryRevision")),
        "effective_date": _text(record.get("effectiveDate")),
        "score": _score(record.get("performanceScore")),
    }


def appraisals_for(employee: dict[str, Any]) -> list[dict[str, Any]]:
    """The employee's appraisal history, newest first. Records without an id
    cannot be tracked across syncs, so they are left out."""
    history = [a for a in employee.get("appraisalHistory") or [] if isinstance(a, dict)]
    entries = [appraisal_entry(a) for a in history if _text(a.get("id"))]
    return sorted(entries, key=lambda e: e["effective_date"] or "", reverse=True)


def asset_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(record.get("id")),
        "name": _text(record.get("assetName")),
        "type": _text(record.get("assetType")),
        "serial_number": _text(record.get("serialNumber")),
        "status": _text(record.get("status")),
        "assigned_date": _text(record.get("assignmentDate")),
    }


def assets_for(employee: dict[str, Any], assigned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assets assigned to this person: the asset register's rows (`assigned`)
    win over copies kept on the employee record, matched by asset id."""
    by_id: dict[str, dict[str, Any]] = {}
    embedded = [a for a in employee.get("assets") or [] if isinstance(a, dict)]
    for record in [*embedded, *assigned]:
        asset_id = _text(record.get("id"))
        if asset_id:
            by_id[asset_id] = asset_entry(record)
    return sorted(by_id.values(), key=lambda e: e["id"] or "")
