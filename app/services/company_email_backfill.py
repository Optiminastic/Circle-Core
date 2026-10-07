"""Move older employee records onto their company mailbox.

Employees converted before onboarding step 6 ("Allocation of mail, system &
desk") was enforced kept the address they applied with as `email`. Every
downstream system (Avora, id-sync, Keycloak) matches people on the company
mailbox, so those records are invisible to them.

The company address HR entered at step 6 is still on the onboarding record as
`allocationEmail`. This module only PLANS the fix (pure, no I/O): which records
move to which address, and which cannot be fixed automatically and why. The
applying is done by scripts/backfill_company_emails.py.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

Document = dict[str, Any]

OFFBOARDED = "Offboarded"

SKIP_NO_ONBOARDING = "no onboarding record"
SKIP_NO_COMPANY_EMAIL = "no company email recorded at onboarding step 6"
SKIP_WRONG_DOMAIN = "onboarding company email is not on the company domain"
SKIP_TAKEN = "company email already belongs to another employee"
SKIP_DUPLICATE = "same company email planned for several employees"


@dataclass(frozen=True)
class EmailMove:
    employee_id: str
    name: str
    old_email: str
    new_email: str


@dataclass(frozen=True)
class EmailSkip:
    employee_id: str
    name: str
    email: str
    reason: str


@dataclass(frozen=True)
class BackfillPlan:
    moves: list[EmailMove]
    skips: list[EmailSkip]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def on_domain(email: str, domain: str) -> bool:
    return _norm(email).endswith("@" + _norm(domain).lstrip("@"))


def _onboarding_for(
    employee: Document,
    by_candidate: Mapping[str, Document],
    by_employee: Mapping[str, Document],
) -> Document | None:
    candidate_id = str(employee.get("candidateId") or "")
    return by_candidate.get(candidate_id) or by_employee.get(str(employee.get("id") or ""))


def plan_backfill(
    employees: Iterable[Document], onboarding: Iterable[Document], *, domain: str
) -> BackfillPlan:
    """Current employees whose `email` is off the company domain, matched to
    the company address on their onboarding record. Nothing is guessed: a
    record without a usable, unclaimed company address is reported, not moved."""
    employees = list(employees)
    onboarding = list(onboarding)
    by_candidate = {str(o.get("candidateId") or o.get("id")): o for o in onboarding}
    by_employee = {str(o["employeeId"]): o for o in onboarding if o.get("employeeId")}
    taken = {_norm(e.get("email")) for e in employees}

    proposed: list[EmailMove] = []
    skips: list[EmailSkip] = []
    for employee in employees:
        email = _norm(employee.get("email"))
        if employee.get("status") == OFFBOARDED or on_domain(email, domain):
            continue
        emp_id = str(employee.get("id") or "")
        name = str(employee.get("fullName") or "")
        record = _onboarding_for(employee, by_candidate, by_employee)
        company = _norm((record or {}).get("allocationEmail"))
        reason = (
            SKIP_NO_ONBOARDING if record is None
            else SKIP_NO_COMPANY_EMAIL if "@" not in company
            else SKIP_WRONG_DOMAIN if not on_domain(company, domain)
            else SKIP_TAKEN if company in taken
            else None
        )
        if reason:
            skips.append(EmailSkip(emp_id, name, email, reason))
        else:
            proposed.append(EmailMove(emp_id, name, email, company))

    counts = Counter(move.new_email for move in proposed)
    moves = [m for m in proposed if counts[m.new_email] == 1]
    skips += [
        EmailSkip(m.employee_id, m.name, m.old_email, SKIP_DUPLICATE)
        for m in proposed
        if counts[m.new_email] > 1
    ]
    return BackfillPlan(moves=moves, skips=skips)


def apply_move(employee: Document, move: EmailMove) -> Document:
    """The employee record after the move. The applied-with address is kept as
    `personalEmail` unless one is already recorded."""
    updated = {**employee, "email": move.new_email}
    if not str(employee.get("personalEmail") or "").strip():
        updated["personalEmail"] = employee.get("email")
    return updated
