"""What Circle hands to Avora about one employee: pay, bank and documents.

This is the ONLY path by which compensation or documents leave Circle, and it
is deliberately separate from the directory export / id-sync push (which carry
directory fields only and feed many apps). It is authenticated with its own
secret (AVORA_API_SECRET), so holding the directory secret grants nothing here.

Circle reports what it has, as it has it; Avora validates and decides.
"""

from __future__ import annotations

import re
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


def compensation_for(employee: dict[str, Any]) -> dict[str, Any]:
    details = employee.get("personalDetails") or {}
    return {
        "employee_code": employee.get("id"),
        "annual_ctc_text": _text(employee.get("annualCtc")),
        "annual_ctc_inr": parse_annual_ctc(employee.get("annualCtc")),
        "pf_enabled": _pf_enabled(employee.get("ctcBreakdown")),
        "joining_date": _text(employee.get("joiningDate")),
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


def is_shareable_document(doc: dict[str, Any], employee_code: str) -> bool:
    """Only this employee's own paperwork - never another entity's file."""
    return (
        doc.get("entityType") == "employee"
        and doc.get("entityId") == employee_code
        and doc.get("category") not in _EXCLUDED_DOCUMENT_CATEGORIES
        and bool(doc.get("storageKey"))
    )
