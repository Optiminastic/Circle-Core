"""When a documents link counts as fully submitted.

`requiredDocs` mixes two different things: files the candidate uploads, and
forms they fill in on the same page (bank details, reference contacts). Only
the files ever become a submission, so the rollup has to treat the two apart.

The rule that shipped did not, and it was unsatisfiable from both sides: asking
for bank details made `issuperset` impossible because "Bank details" is never
an uploaded docType, and not asking for them left `bank_complete` false for
ever. Either way the link stayed `Pending` after the candidate had sent
everything, and HR never saw it was their turn.

Interns are the case that forced it: they are asked for documents and no bank
account at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.routes.doc_requests import (
    BANK_DOC_TYPE,
    REFERENCES_DOC_TYPE,
    is_fully_submitted,
)

INTERN_DOCS = [
    "Aadhaar card",
    "PAN card",
    "Address proof",
    "Education certificates",
    "Passport photo",
    "Previous internship letter",
]
BANK = {"accountNumber": "12345678", "ifscCode": "HDFC0001234"}


def _request(required: list[str], **extra: Any) -> dict[str, Any]:
    return {"requiredDocs": required, **extra}


def test_an_intern_link_completes_without_any_bank_details() -> None:
    # The regression. No bank was asked for, so none can be waited on.
    assert is_fully_submitted(_request(INTERN_DOCS), set(INTERN_DOCS)) is True


def test_a_link_that_asks_for_bank_waits_for_it() -> None:
    required = [*INTERN_DOCS, BANK_DOC_TYPE]
    uploaded = set(INTERN_DOCS)
    assert is_fully_submitted(_request(required), uploaded) is False
    assert is_fully_submitted(_request(required, bankDetails=BANK), uploaded) is True


@pytest.mark.parametrize(
    "bank",
    [None, {}, {"accountNumber": "12345678"}, {"ifscCode": "HDFC0001234"}, {"accountNumber": "", "ifscCode": ""}],
)
def test_half_filled_bank_details_are_not_bank_details(bank: dict[str, str] | None) -> None:
    required = [*INTERN_DOCS, BANK_DOC_TYPE]
    assert is_fully_submitted(_request(required, bankDetails=bank), set(INTERN_DOCS)) is False


def test_a_link_that_asks_for_references_waits_for_one() -> None:
    required = [*INTERN_DOCS, REFERENCES_DOC_TYPE]
    uploaded = set(INTERN_DOCS)
    assert is_fully_submitted(_request(required), uploaded) is False
    assert is_fully_submitted(_request(required, references=[{"fullName": "A"}]), uploaded) is True


def test_a_missing_file_still_holds_the_link_open() -> None:
    assert is_fully_submitted(_request(INTERN_DOCS), set(INTERN_DOCS[:-1])) is False


def test_the_employee_set_still_behaves_as_it_always_should_have() -> None:
    # The full-time path, unchanged in intent: files plus a bank form.
    required = ["Aadhaar card", "PAN card", BANK_DOC_TYPE]
    assert is_fully_submitted(_request(required, bankDetails=BANK), {"Aadhaar card", "PAN card"}) is True


def test_a_link_that_asks_for_nothing_is_never_submitted() -> None:
    # `requiredDocs` empty means the link was built wrong; do not call it done.
    assert is_fully_submitted(_request([]), set()) is False


def test_extra_uploads_do_not_hurt() -> None:
    # A re-issued link can carry documents beyond what it asked for.
    assert is_fully_submitted(_request(INTERN_DOCS), {*INTERN_DOCS, "Cancelled cheque"}) is True
