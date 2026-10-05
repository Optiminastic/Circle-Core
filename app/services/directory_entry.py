"""The directory-safe view of a Circle employee.

Shared by the pull export (/api/directory/export) and the push to id-sync, so
both always send the same narrow set of fields. PAN, Aadhaar, salary, CTC, bank
details and appraisal history never leave Circle.
"""

from __future__ import annotations

from typing import Any


def to_directory_entry(doc: dict[str, Any]) -> dict[str, Any] | None:
    """None when the employee lacks an email or code - nothing to match on."""
    email = (doc.get("email") or "").strip().lower()
    code = (doc.get("id") or "").strip()  # Circle keys employees by their EMP-#### code
    if not email or not code:
        return None
    return {
        "employee_code": code,
        "name": doc.get("fullName") or email,
        "email": email,
        "designation": doc.get("role") or None,   # Circle's "role" is the job title
        "department": doc.get("department") or None,
        "status": doc.get("status") or None,      # Active | On Leave | Suspended | Offboarded
    }
