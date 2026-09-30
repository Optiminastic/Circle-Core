"""Running OnGrid background checks against a candidate's documents.

The shape of this is dictated by how OnGrid actually works, which is not what
the old code assumed:

  * A check is started by its own endpoint - `/v1/individual/{id}/panv` - not by
    listing codes in a `verifications` array. That array is silently discarded.
  * Most checks run against something registered beforehand, and the check body
    only carries that thing's id.

There are four ways a check gets what it needs, and every offering is one of
them:

  document-backed  A document is uploaded to `/doc/{slug}/extract` and OnGrid
                   reads the identity number off it. Those endpoints reject
                   PDFs, so a PDF is rendered to an image first. (PANV)
  self-contained   Nothing from us; OnGrid works from the individual's profile.
                   (CCRV, LAV)
  claim-backed     The candidate states something - a degree, an employment, a
                   permanent address - and OnGrid verifies the claim with the
                   institute, the employer or a field agent. The claim is
                   registered as a record first and the check names its id.
                   (EDUV, EMPV, PAV)
  self-declared    A number the candidate supplies, sent in the check body.
                   (EHC)

`bgv_records` builds the claim-backed records; this module decides which path a
code takes and starts it.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.services import bgv_records
from app.services.ongrid import OnGridClient, OnGridError, UploadFile

logger = get_logger("curcle.bgv_checks")

# (bytes, content type, file name) - how the route hands us a stored file.
DocumentFile = tuple[bytes, str | None, str]

# Offering -> (document type we hold, OnGrid slug that registers it).
CHECK_REQUIREMENTS: dict[str, tuple[str, str]] = {
    "PANV": ("PAN card", "pan"),
}

# Started with an empty body - OnGrid works from the profile it already has.
SELF_CONTAINED_CHECKS: frozenset[str] = frozenset({"CCRV", "LAV"})

# Employment history is verified against EPFO records rather than by contacting
# employers, so the candidate's UAN is the whole input. It must be in the
# request body: a UAN sitting on the individual's profile is not enough, and the
# endpoint answers 500 rather than 400 when it is missing.
UAN_CHECKS: frozenset[str] = frozenset({"EHC"})

# Our joining-document types that are worth attaching to an employment record,
# mapped to the form field OnGrid files them under. All optional - the record
# stands on its fields, and these are corroboration for whoever reviews it.
EMPLOYMENT_PROOF_DOCS: dict[str, str] = {
    "Experience letter": "experienceletter",
    "Offer/appraisal letter": "appointmentletter",
    "Salary slips": "salaryslip",
}

# The document a qualification is registered with.
EDUCATION_DOC_TYPE = "Education certificates"

# Note on Aadhaar: there is no Aadhaar verification to run. OnGrid's ID
# offerings are PANV, DLV (driving licence), PPV (passport) and VIDV (voter ID)
# - no Aadhaar equivalent exists. `AV` is their *address* verification family
# (with LAV, PAV, BAV, XAV), not "Aadhaar Verification", and
# `/v1/individual/{id}/av` answers 404 because no such route exists. Circle's
# catalogue carried an "AV - Aadhaar Card" entry that was never real.
# We still collect and OCR the Aadhaar card; it is identity evidence for HR,
# not something OnGrid can verify.

# Rendered wide enough for OnGrid's OCR without sending a needlessly large file.
_PDF_RENDER_DPI = 200
_JPEG_QUALITY = 90


@dataclass(frozen=True)
class CandidateData:
    """What Circle holds that a check might need, resolved once per run.

    Everything is optional: each check reports for itself what it is missing,
    so one candidate without a UAN does not stop the checks that don't use one.
    """

    uan: str | None = None
    education: Mapping[str, Any] | None = None
    employment: Mapping[str, Any] | None = None
    permanent_address: Mapping[str, Any] | None = None
    references: list[Mapping[str, Any]] = field(default_factory=list)
    education_file: DocumentFile | None = None
    # Keyed by OnGrid form field name, per EMPLOYMENT_PROOF_DOCS.
    employment_files: dict[str, DocumentFile] = field(default_factory=dict)
    # Community configuration rather than anything the candidate knows, but
    # resolved in the same place and needed by the same call.
    prc_schema_id: int | None = None


@dataclass(frozen=True)
class CheckOutcome:
    code: str
    ok: bool
    requestId: str | None = None
    documentId: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ok": self.ok,
            "requestId": self.requestId,
            "documentId": self.documentId,
            "reason": self.reason,
        }


def to_image(data: bytes, content_type: str | None, file_name: str | None) -> tuple[bytes, str]:
    """An image OnGrid's extract endpoints will accept.

    PDFs are rendered; anything already an image passes through untouched. Only
    the extract endpoints need this - `/doc/edu` and `/doc/emprecord` take PDFs.
    """
    is_pdf = data[:5] == b"%PDF-" or (content_type or "").lower() == "application/pdf" or (
        (file_name or "").lower().endswith(".pdf")
    )
    if not is_pdf:
        return data, content_type or "image/jpeg"

    import pypdfium2  # noqa: PLC0415 - optional dependency, same as the OCR path

    pdf = pypdfium2.PdfDocument(data)
    page = pdf[0].render(scale=_PDF_RENDER_DPI / 72).to_pil()
    buffer = io.BytesIO()
    page.convert("RGB").save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue(), "image/jpeg"


# OnGrid reports one `overall{CODE}Status` per offering the platform supports,
# and NOT_REQUESTED for every one this community never asked for - which is most
# of them, so they are dropped rather than shown as rows that mean nothing.
NOT_REQUESTED = "NOT_REQUESTED"
_STATUS_KEY = re.compile(r"^overall([A-Z0-9]+)Status$")


def parse_status(payload: Mapping[str, Any]) -> tuple[str | None, list[dict[str, str]]]:
    """OnGrid's status blob as (overall, one row per started check).

    The states themselves are passed through rather than mapped onto our own
    vocabulary: OnGrid owns what they mean, and inventing a translation here
    would be guessing at values we have not seen.
    """
    started: list[dict[str, str]] = []
    for key, value in payload.items():
        match = _STATUS_KEY.match(key)
        if not match or not value or value == NOT_REQUESTED:
            continue
        started.append({"code": match.group(1), "status": str(value)})
    started.sort(key=lambda row: row["code"])
    overall = payload.get("overallStatus")
    return (str(overall) if overall else None), started


def _missing(code: str, reason: str) -> CheckOutcome:
    return CheckOutcome(code=code, ok=False, reason=reason)


def _start(
    client: OnGridClient,
    individual_id: str,
    code: str,
    payload: dict[str, Any],
    document_id: str | None = None,
) -> CheckOutcome:
    """Call the check's own endpoint and turn the answer into an outcome.

    OnGrid names what it is missing ("Document Id can not be null"), so its
    message is passed through rather than flattened into a generic failure.
    """
    try:
        result = client.request_check(individual_id, code, payload)
    except OnGridError as exc:
        return CheckOutcome(code=code, ok=False, documentId=document_id, reason=str(exc))
    return CheckOutcome(
        code=code,
        ok=True,
        requestId=str(result.get("requestId") or result.get("state") or ""),
        documentId=document_id,
    )


def _record_id(created: Mapping[str, Any]) -> str:
    return str(created.get("id") or "")


def _as_upload(document: DocumentFile, fallback_name: str) -> UploadFile:
    """Storage hands files back as (bytes, type, name); the client wants
    (name, bytes, type). Convert in one place so the two can't drift."""
    raw, content_type, file_name = document
    return (file_name or fallback_name, raw, content_type)


# -- The four paths ------------------------------------------------------------


def _run_uan_check(
    client: OnGridClient, individual_id: str, code: str, data: CandidateData
) -> CheckOutcome:
    if not data.uan:
        return _missing(
            code, "No UAN on file - the candidate can add it in the documents portal."
        )
    return _start(client, individual_id, code, {"uans": [data.uan]})


def _run_self_contained(
    client: OnGridClient, individual_id: str, code: str, _data: CandidateData
) -> CheckOutcome:
    return _start(client, individual_id, code, {})


def _run_document_check(
    client: OnGridClient,
    individual_id: str,
    code: str,
    document: DocumentFile | None,
    document_ids: dict[str, str],
) -> CheckOutcome:
    doc_type, slug = CHECK_REQUIREMENTS[code.upper()]
    document_id = document_ids.get(doc_type)

    if document_id is None:
        if document is None:
            return _missing(code, f"No {doc_type} has been uploaded for this candidate.")
        raw, content_type, file_name = document
        try:
            image, image_type = to_image(raw, content_type, file_name)
            registered = client.upload_for_extract(
                individual_id, slug, file_name or f"{slug}.jpg", image, image_type
            )
        except OnGridError as exc:
            return _missing(code, str(exc))
        except Exception:  # noqa: BLE001 - a bad file must not fail the batch
            logger.exception("Could not prepare %s for OnGrid.", doc_type)
            return _missing(code, f"Could not read the {doc_type} file.")

        document_id = _record_id(registered)
        if not document_id:
            return _missing(code, f"OnGrid did not register the {doc_type}.")
        document_ids[doc_type] = document_id

    return _start(
        client, individual_id, code, {"documentId": int(document_id)}, document_id=document_id
    )


def _run_education(
    client: OnGridClient, individual_id: str, code: str, data: CandidateData
) -> CheckOutcome:
    if not data.education:
        return _missing(
            code,
            "No qualification details on file - the candidate can add them in "
            "the documents portal.",
        )
    if data.education_file is None:
        return _missing(code, f"No {EDUCATION_DOC_TYPE.lower()} have been uploaded.")

    record = bgv_records.education(data.education)
    if not record.ok:
        return _missing(code, f"Qualification details are incomplete: {_list(record.missing)}.")

    try:
        created = client.add_education_document(
            individual_id, record.fields, _as_upload(data.education_file, "certificate.pdf")
        )
    except OnGridError as exc:
        return _missing(code, str(exc))

    record_id = _record_id(created)
    if not record_id:
        return _missing(code, "OnGrid did not register the qualification.")
    return _start(
        client,
        individual_id,
        code,
        {"educationDocumentId": int(record_id)},
        document_id=record_id,
    )


def _run_employment(
    client: OnGridClient, individual_id: str, code: str, data: CandidateData
) -> CheckOutcome:
    if not data.employment:
        return _missing(
            code,
            "No previous employment on file - the candidate can add it in the "
            "documents portal.",
        )

    record = bgv_records.employment(data.employment)
    if not record.ok:
        return _missing(code, f"Employment details are incomplete: {_list(record.missing)}.")

    # OnGrid's schema marks every proof file optional, but the check itself
    # refuses a record without one ("Atleast one document with one scanned copy
    # is required"). Caught here so HR is told which document to ask for,
    # instead of an employment record being created that can never be checked.
    if not data.employment_files:
        return _missing(
            code,
            "No proof of employment has been uploaded - "
            f"{_list(sorted(EMPLOYMENT_PROOF_DOCS))} would each do.",
        )

    proofs = {
        name: _as_upload(document, f"{name}.pdf")
        for name, document in data.employment_files.items()
    }
    try:
        created = client.add_employment_record(individual_id, record.fields, proofs)
    except OnGridError as exc:
        return _missing(code, str(exc))

    record_id = _record_id(created)
    if not record_id:
        return _missing(code, "OnGrid did not register the employment record.")
    return _start(
        client,
        individual_id,
        code,
        {"employmentRecordId": int(record_id)},
        document_id=record_id,
    )


def _run_permanent_address(
    client: OnGridClient, individual_id: str, code: str, data: CandidateData
) -> CheckOutcome:
    if not data.permanent_address:
        return _missing(
            code,
            "No permanent address on file - the candidate can add it in the "
            "documents portal.",
        )

    record = bgv_records.permanent_address(data.permanent_address)
    if not record.ok:
        return _missing(code, f"The permanent address is incomplete: {_list(record.missing)}.")

    try:
        created = client.add_permanent_address(individual_id, record.fields)
    except OnGridError as exc:
        return _missing(code, str(exc))

    address_id = _record_id(created)
    if not address_id:
        return _missing(code, "OnGrid did not return an address id.")
    # PAV is the one check that repeats the individual id in its body.
    return _start(
        client,
        individual_id,
        code,
        {"individualId": int(individual_id), "addressId": int(address_id)},
        document_id=address_id,
    )


def _run_reference(
    client: OnGridClient, individual_id: str, code: str, data: CandidateData
) -> CheckOutcome:
    if data.prc_schema_id is None:
        return _missing(
            code,
            "No reference questionnaire is configured. Ask OnGrid for your "
            "community's reference schema id and set ONGRID_PRC_SCHEMA_ID.",
        )
    if not data.references:
        return _missing(
            code,
            "No references on file - the candidate can add them in the "
            "documents portal.",
        )

    # One referee per check, and one check per code, so the first complete
    # reference is the one used. A second referee is a second PRC.
    attempts = [bgv_records.reference(entry, data.prc_schema_id) for entry in data.references]
    record = next((r for r in attempts if r.ok), None)
    if record is None:
        return _missing(
            code, f"No reference is complete enough: {_list(attempts[0].missing)} missing."
        )

    return _start(client, individual_id, code, record.fields)


ClaimRunner = Callable[[OnGridClient, str, str, CandidateData], CheckOutcome]

CLAIM_CHECKS: dict[str, ClaimRunner] = {
    "EDUV": _run_education,
    "EMPV": _run_employment,
    "PAV": _run_permanent_address,
    # PRC registers nothing first - the referee travels in the check body - but
    # it is the same kind of claim, verified by contacting someone.
    "PRC": _run_reference,
}


def _list(labels: list[str]) -> str:
    return ", ".join(labels)


def run_check(
    client: OnGridClient,
    individual_id: str,
    code: str,
    document: DocumentFile | None,
    document_ids: dict[str, str],
    data: CandidateData,
) -> CheckOutcome:
    """Start one check, whichever way it needs to be started.

    `document_ids` caches registered document ids across the checks in one run,
    so two checks backed by the same document upload it once.
    """
    upper = code.upper()

    if upper in UAN_CHECKS:
        return _run_uan_check(client, individual_id, code, data)
    if upper in SELF_CONTAINED_CHECKS:
        return _run_self_contained(client, individual_id, code, data)
    if upper in CLAIM_CHECKS:
        return CLAIM_CHECKS[upper](client, individual_id, code, data)
    if upper in CHECK_REQUIREMENTS:
        return _run_document_check(client, individual_id, code, document, document_ids)

    return _missing(
        code, f"{code} is not supported from Circle yet - start it in the OnGrid portal."
    )
