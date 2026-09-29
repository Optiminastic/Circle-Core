"""Turning what the candidate typed into the records OnGrid verifies.

Three offerings - EDUV, EMPV and PAV - do not read a document for themselves.
They verify a *claim*: this degree from this institute, this employment at this
employer, this permanent address. The claim has to exist in OnGrid as a record
first, and the check then points at that record's id.

Everything here is pure: candidate data in, OnGrid field names out, plus the
labels of anything mandatory that is missing. No I/O, so the mapping and the
validation can be tested without a network or a database.

Field names and the `level` values are OnGrid's, taken from their API reference
(`reference/EDUV-Education-Verification.json` and the EMPV/PAV equivalents).
Dates are `yyyy-MM-dd` throughout, which is what an `<input type="date">`
produces, so nothing needs reformatting on the way through.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# OnGrid's education levels, verbatim. `POT_GRADUATE_DIPLOMA` is misspelled in
# their API - do not "correct" it, the correct spelling is rejected.
EDUCATION_LEVELS: tuple[str, ...] = (
    "NO_EDUCATION",
    "LESS_THEN_FIFTH_STD",
    "FIFTH_STD",
    "EIGHT_STD",
    "TENTH_STD",
    "TWELFTH_STD",
    "DIPLOMA",
    "GRADUATE",
    "PROFESSIONAL_COURSE",
    "MASTERS",
    "PHD",
    "POST_DOC",
    "POT_GRADUATE_DIPLOMA",
    "OTHER",
    "NA",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEFAULT_COUNTRY = "India"


@dataclass(frozen=True)
class Record:
    """An OnGrid record ready to send, or the reason it isn't.

    `missing` holds human labels, not field names, because it is shown to HR
    as the reason a check could not start.
    """

    fields: dict[str, Any]
    missing: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing


def _text(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    return str(value).strip() if value is not None else ""


def _date(source: Mapping[str, Any], key: str) -> str:
    """A `yyyy-MM-dd` date, or empty if it isn't one.

    A malformed date is dropped rather than forwarded: OnGrid answers 500 on
    input it can't parse, which tells HR nothing.
    """
    value = _text(source, key)
    return value if _DATE_RE.match(value) else ""


def _integer(source: Mapping[str, Any], key: str) -> str:
    value = _text(source, key)
    return value if value.isdigit() else ""


def _collect(
    pairs: list[tuple[str, str, str | None]],
) -> tuple[dict[str, Any], list[str]]:
    """Split (field, value, required-label) triples into what to send and what
    is missing. A blank optional field is omitted rather than sent empty."""
    fields: dict[str, Any] = {}
    missing: list[str] = []
    for name, value, label in pairs:
        if value:
            fields[name] = value
        elif label:
            missing.append(label)
    return fields, missing


def education(entry: Mapping[str, Any]) -> Record:
    """One qualification, for `POST /doc/edu`.

    Mandatory per OnGrid's EDUV page: institute, level, name as printed on the
    certificate, issue date and registration number - plus `degree`, which
    their upload endpoint requires even though the check page omits it.
    """
    level = _text(entry, "level").upper()
    fields, missing = _collect(
        [
            ("level", level if level in EDUCATION_LEVELS else "", "level of education"),
            ("nameOfInstitute", _text(entry, "institute"), "name of institute"),
            ("degree", _text(entry, "degree"), "degree"),
            ("nameAsPerDocument", _text(entry, "nameAsPerDocument"), "name as on the certificate"),
            ("registrationNumber", _text(entry, "registrationNumber"), "registration number"),
            ("issueDate", _date(entry, "issueDate"), "issue date"),
            # Optional, but each one narrows the search the institute has to do.
            ("nameOfBoardUniversity", _text(entry, "boardUniversity"), None),
            ("yearOfPassing", _integer(entry, "yearOfPassing"), None),
            ("fieldOfStudy", _text(entry, "fieldOfStudy"), None),
            ("grade", _text(entry, "grade"), None),
        ]
    )
    return Record(fields=fields, missing=missing)


def employment(entry: Mapping[str, Any]) -> Record:
    """One past employment, for `POST /doc/emprecord`.

    OnGrid marks only the employer and the name on their records mandatory.
    The manager and HR contacts are optional to them but are how the check
    actually reaches someone, so the portal asks for them.
    """
    fields, missing = _collect(
        [
            ("employerName", _text(entry, "employerName"), "employer name"),
            (
                "nameAsPerEmployerRecords",
                _text(entry, "nameAsPerEmployerRecords"),
                "name as per employer records",
            ),
            ("employeeId", _text(entry, "employeeId"), None),
            ("lastDesignation", _text(entry, "designation"), None),
            ("lastWorkingCity", _text(entry, "city"), None),
            ("joiningDate", _date(entry, "joiningDate"), None),
            ("lastWorkingDate", _date(entry, "lastWorkingDate"), None),
            ("managerName", _text(entry, "managerName"), None),
            ("managerEmail", _text(entry, "managerEmail"), None),
            ("managerPhone", _text(entry, "managerPhone"), None),
            ("hrName", _text(entry, "hrName"), None),
            ("hrEmail", _text(entry, "hrEmail"), None),
            ("hrPhone", _text(entry, "hrPhone"), None),
        ]
    )
    return Record(fields=fields, missing=missing)


def permanent_address(entry: Mapping[str, Any]) -> Record:
    """The permanent address, for `POST /permanentaddress`.

    OnGrid's schema marks every part optional, but PAV is a physical visit:
    without a street line, a town, a state and a pincode nobody can be sent
    anywhere, so Circle requires them rather than starting a check that cannot
    complete.
    """
    fields, missing = _collect(
        [
            ("line1", _text(entry, "line1"), "house / street"),
            ("vtc", _text(entry, "city"), "village, town or city"),
            ("state", _text(entry, "state"), "state"),
            ("pincode", _text(entry, "pincode"), "pincode"),
            ("line2", _text(entry, "line2"), None),
            ("locality", _text(entry, "locality"), None),
            ("landmark", _text(entry, "landmark"), None),
            ("district", _text(entry, "district"), None),
        ]
    )
    if missing:
        return Record(fields=fields, missing=missing)

    fields["country"] = _text(entry, "country") or _DEFAULT_COUNTRY
    # `fullAddress` is deliberately not sent. OnGrid composes it from the parts
    # above, and a supplied one is taken verbatim instead - so sending our own
    # is the only way the two could ever disagree.
    return Record(fields=fields, missing=missing)
