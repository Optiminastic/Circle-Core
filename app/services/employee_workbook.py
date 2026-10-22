"""Rows for the full employee directory export (scripts/export_employee_directory.py).

Pure functions over Circle's stored JSON, so the layout is testable without a
database or storage. The script gathers the records; this decides the columns.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.services.avora_export import compensation_for, owner_refs
from app.services.xlsx_writer import Cell, Sheet


def _t(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class EmployeeRecords:
    """Everything stored about one person, gathered from Circle's tables."""

    employee: dict[str, Any]
    onboarding: dict[str, Any] | None = None
    offboarding: dict[str, Any] | None = None
    doc_requests: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)


EMPLOYEE_HEADERS = (
    "Employee code", "Full name", "Status", "Employment type", "Designation", "Department",
    "Work location", "Joining date", "Last working day", "Exit reason",
    "Work email", "Personal email", "Phone",
    "Reporting manager code", "Reporting manager", "Onboarding buddy",
    "Date of birth", "Gender", "Address", "Emergency contact",
    "PAN", "Aadhaar", "UAN",
    "Account holder", "Bank name", "Account number", "IFSC", "Bank details status",
    "Annual CTC (as entered)", "Annual CTC (INR)", "PF applies",
    "Basic (monthly)", "HRA (monthly)", "Special allowance (monthly)",
    "Employer PF (monthly)", "Employee PF (monthly)", "Professional tax (monthly)",
    "Latest appraisal period", "Latest appraisal status", "Revised salary", "Revision effective",
    "Desk", "System", "Assets", "Documents on file",
)


def _bank_submission(records: EmployeeRecords) -> dict[str, Any]:
    """The newest bank details submitted through a document request."""
    found = [r.get("bankDetails") for r in records.doc_requests if isinstance(r.get("bankDetails"), dict)]
    return found[-1] if found else {}


def _uan(records: EmployeeRecords) -> str | None:
    values = [_t(r.get("uan")) for r in records.doc_requests if _t(r.get("uan"))]
    return values[-1] if values else None


def _latest_appraisal(employee: dict[str, Any]) -> dict[str, Any]:
    history = [a for a in employee.get("appraisalHistory") or [] if isinstance(a, dict)]
    return max(history, key=lambda a: str(a.get("effectiveDate") or ""), default={})


def _manager(employee: dict[str, Any], by_code: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    code = _t(employee.get("reportingManagerId"))
    name = _t((by_code.get(code) or {}).get("fullName")) if code else None
    return code, name or _t(employee.get("reportingManager"))


def _assets(records: EmployeeRecords) -> str | None:
    items = [
        " ".join(x for x in (_t(a.get("assetName")) or _t(a.get("assetType")), f"({a['serialNumber']})"
                 if _t(a.get("serialNumber")) else None) if x)
        for a in records.assets
    ]
    return "; ".join(i for i in items if i) or None


def employee_row(records: EmployeeRecords, by_code: dict[str, dict[str, Any]]) -> list[Cell]:
    e = records.employee
    details = e.get("personalDetails") or {}
    onboarding = records.onboarding or {}
    exit_ = records.offboarding or e.get("offboarding") or {}
    comp = compensation_for(e, onboarding.get("offerLetter"))
    submitted = _bank_submission(records)
    breakdown = e.get("ctcBreakdown") or {}
    appraisal = _latest_appraisal(e)
    manager_code, manager_name = _manager(e, by_code)
    buddy = _t(onboarding.get("buddyEmployeeName")) or _t(onboarding.get("buddyEmployeeId"))
    pf = comp["pf_enabled"]
    return [
        _t(e.get("id")), _t(e.get("fullName")), _t(e.get("status")), _t(e.get("employmentType")),
        _t(e.get("role")), _t(e.get("department")), _t(e.get("workLocation")),
        _t(e.get("joiningDate")) or comp["joining_date"],
        _t(exit_.get("lastWorkingDay")), _t(exit_.get("triggerReason")),
        _t(e.get("email")), _t(e.get("personalEmail")), _t(e.get("phone")),
        manager_code, manager_name, buddy,
        _t(details.get("dateOfBirth")), _t(details.get("gender")),
        _t(details.get("address")), _t(details.get("emergencyContact")),
        _t(details.get("panNumber")), _t(details.get("aadhaarNumber")), _uan(records),
        _t(submitted.get("accountHolderName")),
        _t(details.get("bankName")) or _t(submitted.get("bankName")),
        _t(details.get("accountNumber")) or _t(submitted.get("accountNumber")) or _t(details.get("bankAccount")),
        _t(details.get("ifsc")) or _t(submitted.get("ifscCode")),
        _t(submitted.get("status")) or ("Submitted" if submitted else None),
        comp["annual_ctc_text"], comp["annual_ctc_inr"], None if pf is None else ("Yes" if pf else "No"),
        _num(breakdown.get("basic")), _num(breakdown.get("hra")), _num(breakdown.get("specialAllowance")),
        _num(breakdown.get("employerPf")), _num(breakdown.get("employeePf")),
        _num(breakdown.get("professionalTax")),
        _t(appraisal.get("reviewPeriod")), _t(appraisal.get("status")),
        _t(appraisal.get("recommendedSalaryRevision")), _t(appraisal.get("effectiveDate")),
        _t(onboarding.get("deskNumber")),
        " ".join(x for x in (_t(onboarding.get("systemName")), _t(onboarding.get("systemNumber"))) if x) or None,
        _assets(records), len(records.documents),
    ]


DOCUMENT_HEADERS = (
    "Employee code", "Full name", "Category", "File name", "Review status",
    "Uploaded", "Size (KB)", "Filed under", "Link",
)
DOCUMENT_LINK_COLUMN = DOCUMENT_HEADERS.index("Link")


def _review_status(records: EmployeeRecords) -> dict[str, str]:
    """documentId -> HR's review status, from the requests it was submitted to."""
    status: dict[str, str] = {}
    for request in records.doc_requests:
        for sub in request.get("submissions") or []:
            if isinstance(sub, dict) and sub.get("documentId"):
                status[str(sub["documentId"])] = _t(sub.get("status")) or "Submitted"
    return status


def document_rows(records: EmployeeRecords, link_for: Callable[[dict[str, Any]], str | None]) -> list[list[Cell]]:
    e = records.employee
    reviews = _review_status(records)
    rows: list[list[Cell]] = []
    for doc in sorted(records.documents, key=lambda d: str(d.get("uploadedAt") or "")):
        size = _num(doc.get("size"))
        rows.append([
            _t(e.get("id")), _t(e.get("fullName")), _t(doc.get("category")), _t(doc.get("fileName")),
            reviews.get(str(doc.get("id")), "Added by HR"), _t(doc.get("uploadedAt")),
            round(size / 1024, 1) if size else None,
            "Candidate record" if doc.get("entityType") == "candidate" else "Employee record",
            link_for(doc),
        ])
    return rows


APPRAISAL_HEADERS = (
    "Employee code", "Full name", "Review period", "Status", "Current salary",
    "Recommended revision", "Recommended promotion", "Final decision", "Effective date",
    "Performance score",
)


def appraisal_rows(employee: dict[str, Any]) -> list[list[Cell]]:
    history = [a for a in employee.get("appraisalHistory") or [] if isinstance(a, dict)]
    return [
        [
            _t(employee.get("id")), _t(employee.get("fullName")), _t(a.get("reviewPeriod")),
            _t(a.get("status")), _t(a.get("currentSalary")), _t(a.get("recommendedSalaryRevision")),
            _t(a.get("recommendedPromotion")), _t(a.get("finalDecision")), _t(a.get("effectiveDate")),
            _num(a.get("performanceScore")),
        ]
        for a in sorted(history, key=lambda a: str(a.get("effectiveDate") or ""))
    ]


def build_sheets(
    people: Iterable[EmployeeRecords], link_for: Callable[[dict[str, Any]], str | None]
) -> list[Sheet]:
    people = sorted(people, key=lambda p: str(p.employee.get("id") or ""))
    by_code = {str(p.employee.get("id")): p.employee for p in people}
    return [
        Sheet("Employees", EMPLOYEE_HEADERS, [employee_row(p, by_code) for p in people]),
        Sheet(
            "Documents",
            DOCUMENT_HEADERS,
            [row for p in people for row in document_rows(p, link_for)],
            link_columns=frozenset({DOCUMENT_LINK_COLUMN}),
        ),
        Sheet("Appraisals", APPRAISAL_HEADERS, [row for p in people for row in appraisal_rows(p.employee)]),
    ]


def documents_of(employee: dict[str, Any], all_docs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every file stored under this person's employee or candidate record."""
    refs = owner_refs(employee)
    return [d for d in all_docs if (d.get("entityType"), d.get("entityId")) in refs and d.get("storageKey")]
