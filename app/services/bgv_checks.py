"""Running OnGrid background checks against a candidate's documents.

The shape of this is dictated by how OnGrid actually works, which is not what
the old code assumed:

  * A check is started by its own endpoint - `/v1/individual/{id}/panv` - not by
    listing codes in a `verifications` array. That array is silently discarded.
  * A check runs against a *registered document*, not against values we send.
    `/doc/{slug}/extract` registers one and OnGrid reads the number off it
    itself; `/doc/other` only stores a file, so a check against it fails with
    "No PAN found to initiate PANV."
  * Those extract endpoints reject PDFs, so a PDF is rendered to an image first.

So each check needs a document, uploaded to the right slug, and the document id
that comes back.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.services.ongrid import OnGridClient, OnGridError

logger = get_logger("curcle.bgv_checks")

# What each offering needs, established by calling its endpoint and reading what
# it complains about. Three kinds:
#
#   document-backed  a registered document - the check runs against what OnGrid
#                    extracted from it
#   self-contained   nothing from us; OnGrid uses the individual's own profile
#   unavailable      needs a record Circle does not hold, or has no endpoint

# Offering -> (document type we hold, OnGrid slug that registers it).
CHECK_REQUIREMENTS: dict[str, tuple[str, str]] = {
    "PANV": ("PAN card", "pan"),
}

# Started with an empty body - OnGrid works from the profile it already has.
SELF_CONTAINED_CHECKS: frozenset[str] = frozenset({"CCRV", "LAV"})

# Why a check can't be started from here. Shown to HR verbatim, so each says
# what to do instead rather than just failing.
UNAVAILABLE_CHECKS: dict[str, str] = {
    "AV": (
        "Aadhaar verification has no API endpoint - run it from the OnGrid portal."
    ),
    "EMPV": (
        "Employment verification needs an employment record in OnGrid, which "
        "Circle does not create yet - add it in the portal first."
    ),
    # POST /v1/individual/{id}/doc/edu exists and takes multipart, but needs
    # qualification metadata alongside the file (educationLevel is one; it
    # rejects POST_GRADUATE and accepts POSTGRADUATE). The remaining required
    # fields are on OnGrid's "Add Education Document" page. Once known, move
    # this back into CHECK_REQUIREMENTS with those fields.
    "EDUV": (
        "Education verification needs the qualification details alongside the "
        "certificate, which Circle does not collect yet - add it in the portal."
    ),
    "PRC": (
        "Reference check needs a reference schema selected in OnGrid - "
        "configure it in the portal first."
    ),
    "PAV": (
        "Permanent address verification needs a permanent address on the "
        "OnGrid profile, which Circle does not send."
    ),
    "EHC": (
        "Employment history check is failing inside OnGrid - raise it with them."
    ),
}

# Rendered wide enough for OnGrid's OCR without sending a needlessly large file.
_PDF_RENDER_DPI = 200
_JPEG_QUALITY = 90


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

    PDFs are rendered; anything already an image passes through untouched.
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


def run_check(
    client: OnGridClient,
    individual_id: str,
    code: str,
    document: tuple[bytes, str | None, str] | None,
    document_ids: dict[str, str],
) -> CheckOutcome:
    """Register the document this check needs (once per document type), then
    start the check. `document_ids` caches ids across checks in one run."""
    upper = code.upper()

    if upper in UNAVAILABLE_CHECKS:
        return CheckOutcome(code=code, ok=False, reason=UNAVAILABLE_CHECKS[upper])

    # Nothing to attach: OnGrid runs these off the profile it already holds.
    if upper in SELF_CONTAINED_CHECKS:
        try:
            result = client.request_check(individual_id, code, {})
        except OnGridError as exc:
            return CheckOutcome(code=code, ok=False, reason=str(exc))
        return CheckOutcome(
            code=code, ok=True, requestId=str(result.get("requestId") or result.get("state") or "")
        )

    requirement = CHECK_REQUIREMENTS.get(upper)
    if requirement is None:
        return CheckOutcome(
            code=code,
            ok=False,
            reason=f"{code} is not supported from Circle yet - start it in the OnGrid portal.",
        )

    doc_type, slug = requirement
    document_id = document_ids.get(doc_type)

    if document_id is None:
        if document is None:
            return CheckOutcome(
                code=code, ok=False, reason=f"No {doc_type} has been uploaded for this candidate."
            )
        data, content_type, file_name = document
        try:
            image, image_type = to_image(data, content_type, file_name)
            registered = client.upload_for_extract(
                individual_id, slug, file_name or f"{slug}.jpg", image, image_type
            )
        except OnGridError as exc:
            return CheckOutcome(code=code, ok=False, reason=str(exc))
        except Exception:  # noqa: BLE001 - a bad file must not fail the batch
            logger.exception("Could not prepare %s for OnGrid.", doc_type)
            return CheckOutcome(code=code, ok=False, reason=f"Could not read the {doc_type} file.")

        document_id = str(registered.get("id") or "")
        if not document_id:
            return CheckOutcome(
                code=code, ok=False, reason=f"OnGrid did not register the {doc_type}."
            )
        document_ids[doc_type] = document_id

    try:
        result = client.request_check(individual_id, code, {"documentId": int(document_id)})
    except OnGridError as exc:
        # OnGrid names what it is missing, e.g. "'AV' not supported." - pass it
        # through rather than flattening it into a generic failure.
        return CheckOutcome(code=code, ok=False, documentId=document_id, reason=str(exc))

    return CheckOutcome(
        code=code,
        ok=True,
        requestId=str(result.get("requestId") or ""),
        documentId=document_id,
    )
