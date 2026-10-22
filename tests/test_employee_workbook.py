"""The employee directory export: the columns it fills, and a valid .xlsx."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

from app.services.employee_workbook import (
    DOCUMENT_HEADERS,
    EMPLOYEE_HEADERS,
    EmployeeRecords,
    build_sheets,
    documents_of,
)
from app.services.xlsx_writer import Sheet, column_letter, write_workbook

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _manager() -> dict[str, object]:
    return {"id": "EMP-1001", "fullName": "Usama Khan", "status": "Active"}


def _joiner() -> dict[str, object]:
    return {
        "id": "EMP-1002",
        "fullName": "Dhriti Shah",
        "status": "Active",
        "role": "Designer",
        "email": "dhriti@corp.io",
        "candidateId": "CAND-9",
        "reportingManagerId": "EMP-1001",
        "reportingManager": "Old typed name",
        "annualCtc": "6 LPA",
        "ctcBreakdown": {"basic": 25000, "hra": 10000, "specialAllowance": 12000,
                         "employerPf": 1800, "employeePf": 1800, "professionalTax": 200},
        "personalDetails": {"panNumber": "ABCDE1234F", "dateOfBirth": "2000-01-02"},
        "appraisalHistory": [
            {"reviewPeriod": "H1 2026", "effectiveDate": "2026-04-01", "status": "Approved",
             "recommendedSalaryRevision": "7 LPA"},
            {"reviewPeriod": "Annual 2025", "effectiveDate": "2025-04-01", "status": "Completed"},
        ],
    }


def _records() -> list[EmployeeRecords]:
    joiner = _joiner()
    docs = [
        {"id": "d1", "entityType": "candidate", "entityId": "CAND-9", "category": "PAN Card",
         "fileName": "pan.pdf", "storageKey": "k1", "uploadedAt": "2026-01-01", "size": 2048},
        {"id": "d2", "entityType": "employee", "entityId": "EMP-1002", "category": "Appraisal letter",
         "fileName": "letter.pdf", "storageKey": "k2", "uploadedAt": "2026-05-01"},
        {"id": "d3", "entityType": "employee", "entityId": "EMP-1001", "fileName": "someone-else.pdf",
         "storageKey": "k3"},
    ]
    request = {
        "candidateId": "CAND-9",
        "uan": "100200300400",
        "bankDetails": {"accountHolderName": "Dhriti Shah", "bankName": "HDFC",
                        "accountNumber": "5010", "ifscCode": "HDFC0001", "status": "Verified"},
        "submissions": [{"documentId": "d1", "status": "Verified"}],
    }
    return [
        EmployeeRecords(employee=_manager()),
        EmployeeRecords(
            employee=joiner,
            onboarding={"buddyEmployeeName": "Anup Shirke", "deskNumber": "D-4"},
            offboarding={"lastWorkingDay": "2026-12-31", "triggerReason": "Resignation"},
            doc_requests=[request],
            documents=documents_of(joiner, docs),
            assets=[{"assetName": "MacBook Air", "serialNumber": "SN1"}],
        ),
    ]


def _row(sheet: Sheet, code: str) -> dict[str, object]:
    row = next(r for r in sheet.rows if r[0] == code)
    return dict(zip(sheet.headers, row, strict=True))


def test_an_employee_row_has_manager_bank_ctc_and_appraisal() -> None:
    employees = build_sheets(_records(), lambda doc: None)[0]
    row = _row(employees, "EMP-1002")

    assert len(employees.headers) == len(EMPLOYEE_HEADERS)
    assert row["Reporting manager"] == "Usama Khan"  # resolved from the code, not the typed name
    assert row["Onboarding buddy"] == "Anup Shirke"
    assert row["Last working day"] == "2026-12-31"
    assert row["Bank name"] == "HDFC" and row["Account number"] == "5010"
    assert row["IFSC"] == "HDFC0001" and row["Bank details status"] == "Verified"
    assert row["UAN"] == "100200300400" and row["PAN"] == "ABCDE1234F"
    assert row["Annual CTC (INR)"] == 600000 and row["PF applies"] == "Yes"
    assert row["Basic (monthly)"] == 25000
    assert row["Latest appraisal period"] == "H1 2026" and row["Revised salary"] == "7 LPA"
    assert row["Assets"] == "MacBook Air (SN1)"
    assert row["Documents on file"] == 2


def test_documents_are_only_the_persons_own_with_status_and_link() -> None:
    documents = build_sheets(_records(), lambda doc: f"https://files.example/{doc['id']}")[1]

    assert [r[3] for r in documents.rows] == ["pan.pdf", "letter.pdf"]  # never EMP-1001's file
    pan = dict(zip(DOCUMENT_HEADERS, documents.rows[0], strict=True))
    assert pan["Review status"] == "Verified"
    assert pan["Filed under"] == "Candidate record"
    assert pan["Link"] == "https://files.example/d1"
    assert pan["Size (KB)"] == 2.0


def test_the_workbook_is_a_valid_xlsx_with_links(tmp_path: Path) -> None:
    out = tmp_path / "directory.xlsx"
    sheets = build_sheets(_records(), lambda doc: f"https://files.example/{doc['id']}?a=1&b=2")
    sheets[0].rows.append(["=HYPERLINK(\"x\")", "bad\x07char"])  # formula text, control char

    write_workbook(str(out), sheets)

    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(z.read(name))  # every part is well-formed XML
        sheet1 = ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))
        assert sheet1.find(".//m:c/m:f", NS) is None  # nothing is ever a formula
        rels = z.read("xl/worksheets/_rels/sheet2.xml.rels").decode()
        assert "https://files.example/d1?a=1&amp;b=2" in rels


def test_column_letters() -> None:
    assert [column_letter(i) for i in (0, 25, 26, 51, 52)] == ["A", "Z", "AA", "AZ", "BA"]
