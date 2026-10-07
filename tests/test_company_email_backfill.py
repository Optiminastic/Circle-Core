"""Planning the move of older employees onto their company mailbox."""

from __future__ import annotations

from app.services.company_email_backfill import (
    SKIP_DUPLICATE,
    SKIP_NO_COMPANY_EMAIL,
    SKIP_NO_ONBOARDING,
    SKIP_TAKEN,
    SKIP_WRONG_DOMAIN,
    EmailMove,
    apply_move,
    plan_backfill,
)

DOMAIN = "corp.io"


def _emp(emp_id: str, email: str, cand: str | None = None, **extra: object) -> dict[str, object]:
    return {"id": emp_id, "fullName": emp_id, "email": email, "candidateId": cand, "status": "Active", **extra}


def _onb(cand: str, allocation: str | None, employee_id: str | None = None) -> dict[str, object]:
    return {"id": cand, "candidateId": cand, "allocationEmail": allocation, "employeeId": employee_id}


def _reasons(plan) -> dict[str, str]:
    return {s.employee_id: s.reason for s in plan.skips}


def test_moves_personal_email_to_onboarding_company_email() -> None:
    plan = plan_backfill([_emp("E1", "Me@Gmail.com", "C1")], [_onb("C1", " New@Corp.io ")], domain=DOMAIN)
    assert plan.moves == [EmailMove("E1", "E1", "me@gmail.com", "new@corp.io")]
    assert plan.skips == []


def test_falls_back_to_onboarding_linked_by_employee_id() -> None:
    plan = plan_backfill([_emp("E1", "me@gmail.com")], [_onb("C9", "e1@corp.io", employee_id="E1")], domain=DOMAIN)
    assert [m.new_email for m in plan.moves] == ["e1@corp.io"]


def test_leaves_company_and_offboarded_records_alone() -> None:
    employees = [_emp("E1", "a@corp.io", "C1"), _emp("E2", "b@gmail.com", "C2", status="Offboarded")]
    plan = plan_backfill(employees, [_onb("C1", "x@corp.io"), _onb("C2", "y@corp.io")], domain=DOMAIN)
    assert plan.moves == [] and plan.skips == []


def test_reports_what_it_cannot_fix() -> None:
    employees = [
        _emp("E1", "a@gmail.com", "C1"),
        _emp("E2", "b@gmail.com", "C2"),
        _emp("E3", "c@gmail.com", "C3"),
        _emp("E4", "d@gmail.com", "C4"),
        _emp("E5", "taken@corp.io"),
    ]
    onboarding = [_onb("C2", None), _onb("C3", "c@other.com"), _onb("C4", "taken@corp.io")]
    plan = plan_backfill(employees, onboarding, domain=DOMAIN)
    assert plan.moves == []
    assert _reasons(plan) == {
        "E1": SKIP_NO_ONBOARDING,
        "E2": SKIP_NO_COMPANY_EMAIL,
        "E3": SKIP_WRONG_DOMAIN,
        "E4": SKIP_TAKEN,
    }


def test_never_gives_two_people_the_same_address() -> None:
    employees = [_emp("E1", "a@gmail.com", "C1"), _emp("E2", "b@gmail.com", "C2")]
    plan = plan_backfill(employees, [_onb("C1", "same@corp.io"), _onb("C2", "Same@corp.io")], domain=DOMAIN)
    assert plan.moves == []
    assert _reasons(plan) == {"E1": SKIP_DUPLICATE, "E2": SKIP_DUPLICATE}


def test_domain_must_match_exactly() -> None:
    plan = plan_backfill([_emp("E1", "a@gmail.com", "C1")], [_onb("C1", "a@notcorp.io")], domain=DOMAIN)
    assert _reasons(plan) == {"E1": SKIP_WRONG_DOMAIN}


def test_apply_keeps_the_old_address_as_personal_email() -> None:
    move = EmailMove("E1", "E1", "a@gmail.com", "a@corp.io")
    assert apply_move({"id": "E1", "email": "a@gmail.com"}, move) == {
        "id": "E1", "email": "a@corp.io", "personalEmail": "a@gmail.com",
    }
    kept = apply_move({"id": "E1", "email": "a@gmail.com", "personalEmail": "first@x.com"}, move)
    assert kept["personalEmail"] == "first@x.com"
