"""Export the full employee directory to an Excel workbook (HR/admin use).

Sheets: Employees (one row per person: profile, reporting manager, buddy, exit,
IDs, bank details, CTC and its monthly breakdown, latest appraisal, assets),
Documents (every file on their employee and candidate records, with HR's review
status and a download link), and Appraisals (full history).

    python -m scripts.export_employee_directory /tmp/circle-employees.xlsx
    python -m scripts.export_employee_directory out.xlsx --link-days 1
    python -m scripts.export_employee_directory out.xlsx --no-links

The file holds bank details, IDs and salaries in plain text: it is written
readable by its owner only, and nothing personal is printed to the terminal.
Document links are presigned storage URLs: anyone holding the file can open
them until they expire (default 3 days, at most 7), so keep it private.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from typing import Any

from app.core.config import get_settings
from app.db.database import Database
from app.repositories.document_repository import SqlAlchemyDocumentRepository
from app.services.employee_workbook import EmployeeRecords, build_sheets, documents_of
from app.services.xlsx_writer import write_workbook

DEFAULT_LINK_DAYS = 3
MAX_LINK_DAYS = 7  # S3 signature limit
SECONDS_PER_DAY = 86_400
_UNSAFE_FILENAME = re.compile(r'["\r\n\\]')


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", help="Path of the .xlsx to write")
    parser.add_argument("--link-days", type=int, default=DEFAULT_LINK_DAYS,
                        help=f"How long document links work (1-{MAX_LINK_DAYS} days)")
    parser.add_argument("--no-links", action="store_true", help="Leave the Link column empty")
    args = parser.parse_args()
    if not 1 <= args.link_days <= MAX_LINK_DAYS:
        parser.error(f"--link-days must be between 1 and {MAX_LINK_DAYS}")
    return args


def _by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get(key):
            grouped[str(row[key])].append(row)
    return grouped


def _gather(repo: SqlAlchemyDocumentRepository) -> list[EmployeeRecords]:
    """Every table read once and joined in memory (no per-employee queries)."""
    employees = repo.list("employees")
    onboarding = repo.list("onboarding")
    onboarding_by_candidate = {str(o.get("candidateId") or o.get("id")): o for o in onboarding}
    onboarding_by_employee = {str(o["employeeId"]): o for o in onboarding if o.get("employeeId")}
    offboarding = {str(o["employeeId"]): o for o in repo.list("offboarding") if o.get("employeeId")}
    requests = _by(repo.list("doc_requests"), "candidateId")
    assets = _by(repo.list("assets"), "assignedToEmployeeId")
    documents = repo.list("documents")
    people = []
    for e in employees:
        code, candidate = str(e.get("id") or ""), str(e.get("candidateId") or "")
        people.append(EmployeeRecords(
            employee=e,
            onboarding=onboarding_by_candidate.get(candidate) or onboarding_by_employee.get(code),
            offboarding=offboarding.get(code),
            doc_requests=requests.get(candidate, []) + requests.get(code, []),
            documents=documents_of(e, documents),
            assets=assets.get(code, []),
        ))
    return people


def _link_maker(settings: Any, days: int | None):
    if days is None or not settings.has_storage:
        return lambda doc: None
    from app.storage.s3_storage import S3FileStorage

    storage = S3FileStorage(settings)

    def link(doc: dict[str, Any]) -> str | None:
        name = _UNSAFE_FILENAME.sub("_", str(doc.get("fileName") or "document"))
        return storage.presigned_url(
            str(doc["storageKey"]), expires=days * SECONDS_PER_DAY,
            disposition=f'inline; filename="{name}"', content_type=doc.get("contentType"),
        )

    return link


def main() -> None:
    args = _args()
    settings = get_settings()
    database = Database(settings)
    database.connect()
    session = database.session()
    try:
        people = _gather(SqlAlchemyDocumentRepository(session))
    finally:
        session.close()
        database.dispose()
    link_for = _link_maker(settings, None if args.no_links else args.link_days)
    sheets = build_sheets(people, link_for)
    # Create it owner-only before any personal data is written into it.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    write_workbook(args.output, sheets)
    print(f"Wrote {args.output}: {len(sheets[0].rows)} employees, {len(sheets[1].rows)} documents, "
          f"{len(sheets[2].rows)} appraisal records.")
    if not settings.has_storage and not args.no_links:
        print("Storage is not configured here, so the document Link column is empty.")


if __name__ == "__main__":
    main()
