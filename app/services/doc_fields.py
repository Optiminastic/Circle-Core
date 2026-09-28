"""Structured field extraction from OCR'd joining documents.

Pure functions: OCR text in, structured fields out. No I/O, no settings, no
logging of values - so this module is safe to unit-test in isolation and cannot
leak anything on its own.

Each document type carries a cheap *self-validating* signal, which is what makes
"did the OCR read this correctly?" answerable without a human:

  * Aadhaar - 12 digits that must satisfy the Verhoeff checksum. A single
    misread digit fails it, which is exactly the blurry-photo case.
  * PAN     - a strict AAAAA9999A shape.

Address proofs and education certificates have no such signal and wildly
variable layouts, so only the pincode / year are treated as extractions; the
rest are hints for HR to correct.

An Aadhaar number is masked here, before it is returned, so no caller can
persist or log the full value (DPDP - we only ever need the last 4 to let HR
confirm they are looking at the right document).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Document types, matching the frontend `RequiredDocType` catalogue in
# circle-fe/lib/onboarding-docs.ts. Anything else extracts nothing.
AADHAAR = "Aadhaar card"
PAN = "PAN card"
ADDRESS_PROOF = "Address proof"
EDUCATION = "Education certificates"

SUPPORTED_DOC_TYPES = (AADHAAR, PAN, ADDRESS_PROOF, EDUCATION)

_AADHAAR_DIGITS = 12
_PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")
# A whole run of digits, including the spaces/hyphens Aadhaar is grouped with.
# Matching the run and then counting is what separates a real 12-digit Aadhaar
# from the 16-digit VID and 28-digit EID also printed on modern cards: a
# "12 digits" pattern matches happily *inside* both of those.
# Spaces and tabs only, never newlines - otherwise a number ending one line and
# another starting the next merge into a single run and both are lost.
_DIGIT_RUN_RE = re.compile(r"\d[\d \t-]*\d")
# UIDAI never issues a number starting 0 or 1, so those are a misread.
_AADHAAR_INVALID_FIRST = ("0", "1")
_PINCODE_RE = re.compile(r"\b([1-9]\d{5})\b")
_DOB_RE = re.compile(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b")
# A name-shaped word: starts with a letter, no digits. Rules out OCR noise like
# "2s" without rejecting hyphenated or apostrophed names.
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z.'-]+")
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

# Degree years outside this range are almost certainly a misread.
_YEAR_MIN = 1950
_YEAR_MAX = 2049

# Word-boundary matched, never substring: "FEMALE" contains "MALE", so a plain
# `in` test reports every woman as male.
_GENDER_PATTERNS = (
    (re.compile(r"(?<!\w)FEMALE(?!\w)", re.IGNORECASE), "Female"),
    (re.compile(r"(?<!\w)TRANSGENDER(?!\w)", re.IGNORECASE), "Other"),
    (re.compile(r"(?<!\w)MALE(?!\w)", re.IGNORECASE), "Male"),
    # Devanagari, as printed on Aadhaar alongside the English.
    (re.compile(r"स्त्री"), "Female"),
    (re.compile(r"पुरुष"), "Male"),
)

_DEGREE_HINTS = (
    "bachelor", "master", "b.tech", "b.e", "b.sc", "b.com", "b.a",
    "m.tech", "m.sc", "m.com", "m.a", "mba", "mca", "bca", "phd", "diploma",
)
_INSTITUTION_HINTS = ("university", "institute", "college", "board", "school")


def _hint_patterns(hints: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Word-boundary matchers for each hint. Substring matching is wrong here:
    "mba" occurs inside "Mumbai", which would tag a university as a degree."""
    return tuple(
        re.compile(r"(?<!\w)" + re.escape(hint) + r"(?!\w)", re.IGNORECASE)
        for hint in hints
    )


_DEGREE_PATTERNS = _hint_patterns(_DEGREE_HINTS)
_INSTITUTION_PATTERNS = _hint_patterns(_INSTITUTION_HINTS)


@dataclass(frozen=True)
class FieldResult:
    """Extracted values plus whether the document's own format check passed."""

    fields: dict[str, str]
    warnings: list[str]
    validated: bool


# --- Aadhaar checksum ---------------------------------------------------------
# Verhoeff: dihedral-group checksum used by UIDAI. Tables are from the published
# algorithm; treat them as data, not something to simplify.
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def is_valid_aadhaar(number: str) -> bool:
    """True when `number` is a plausible Aadhaar: 12 digits, not starting 0 or
    1, and satisfying the Verhoeff checksum."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) != _AADHAAR_DIGITS:
        return False
    if digits[0] in _AADHAAR_INVALID_FIRST:
        return False
    checksum = 0
    for position, digit in enumerate(reversed(digits)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[position % 8][int(digit)]]
    return checksum == 0


def mask_aadhaar(number: str) -> str:
    """"123412341234" -> "XXXXXXXX1234". Only the last 4 digits are kept."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) != _AADHAAR_DIGITS:
        return ""
    return "X" * (_AADHAAR_DIGITS - 4) + digits[-4:]


# --- Shared helpers -----------------------------------------------------------


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    return match.group(1) if match else ""


def _labelled_value(lines: list[str], *labels: str) -> str:
    """Value printed after a label, either on the same line ("Name: X") or on
    the next one, which is how most Indian ID cards lay it out."""
    for index, line in enumerate(lines):
        lowered = line.lower()
        for label in labels:
            if label not in lowered:
                continue
            _, _, rest = line.partition(":")
            if rest.strip():
                return rest.strip()
            if index + 1 < len(lines):
                return lines[index + 1]
    return ""


def _name_above_dob(lines: list[str]) -> str:
    """The holder's name, taken from the line directly above the date of birth.

    Aadhaar has no "Name:" label - on the card itself the name is printed bare,
    immediately above the DOB, so the date is the only dependable anchor. The
    postal address block higher up the page repeats the name, but that one sits
    under "To" among address lines and is far easier to grab wrongly.
    """
    for index, line in enumerate(lines):
        if index and _DOB_RE.search(line):
            return _clean_name(lines[index - 1])
    return ""


def _clean_name(line: str) -> str:
    """Strip the OCR noise that the Devanagari line above bleeds into the name
    (e.g. "2s Sakshi Eknath Sawant"). Empty unless at least two name-shaped
    words survive, so a garbled line yields nothing rather than a wrong name.
    """
    tokens = [token for token in (line or "").split() if _NAME_TOKEN_RE.fullmatch(token)]
    return " ".join(tokens) if len(tokens) >= 2 else ""


def _find_gender(text: str) -> str:
    """Female and Transgender are tested before Male, so a partial match on the
    tail of a longer word can never win."""
    for pattern, value in _GENDER_PATTERNS:
        if pattern.search(text or ""):
            return value
    return ""


# --- Per-document extractors --------------------------------------------------


def _find_aadhaar_candidate(text: str) -> str:
    """The best 12-digit run in the text, ignoring grouping.

    Length is checked on the whole run, so a 16-digit VID or 28-digit EID is
    rejected outright rather than yielding its first 12 digits.

    A real card carries several 12-digit runs - the number is printed on both
    sides and sits near enrolment/reference numbers of the same length - and OCR
    reads some of them wrongly. So prefer one that satisfies the checksum over
    whichever happens to appear first; taking the first match returns a number
    that is merely 12 digits long, not the person's Aadhaar.

    Falls back to the first run when none validates, so HR still sees something
    to correct rather than an empty field.
    """
    candidates = [
        digits
        for match in _DIGIT_RUN_RE.finditer(text or "")
        if len(digits := re.sub(r"\D", "", match.group(0))) == _AADHAAR_DIGITS
    ]
    for digits in candidates:
        if is_valid_aadhaar(digits):
            return digits
    return candidates[0] if candidates else ""


def _extract_aadhaar(text: str) -> FieldResult:
    raw = _find_aadhaar_candidate(text)
    warnings: list[str] = []
    fields: dict[str, str] = {}

    if not raw:
        warnings.append("No 12-digit Aadhaar number found in the image.")
        validated = False
    elif not is_valid_aadhaar(raw):
        # Digits were read but the checksum rejects them - almost always a
        # misread digit rather than a genuinely invalid card.
        warnings.append("Aadhaar checksum did not match - a digit was likely misread.")
        fields["number"] = mask_aadhaar(raw)
        validated = False
    else:
        fields["number"] = mask_aadhaar(raw)
        validated = True

    lines = _lines(text)
    dob = _first(_DOB_RE, text)
    if dob:
        fields["dob"] = dob
    gender = _find_gender(text)
    if gender:
        fields["gender"] = gender
    # The card's own name line first; the "To" address block only as a fallback.
    name = _name_above_dob(lines) or _labelled_value(lines, "name")
    if name:
        fields["name"] = name

    return FieldResult(fields=fields, warnings=warnings, validated=validated)


def _extract_pan(text: str) -> FieldResult:
    upper = (text or "").upper()
    number = _first(_PAN_RE, upper)
    warnings: list[str] = []
    fields: dict[str, str] = {}

    if number:
        fields["number"] = number
        validated = True
    else:
        warnings.append("No valid PAN number (ABCDE1234F) found in the image.")
        validated = False

    lines = _lines(text)
    name = _labelled_value(lines, "name")
    if name:
        fields["name"] = name
    father = _labelled_value(lines, "father")
    if father:
        fields["fatherName"] = father
    dob = _first(_DOB_RE, text)
    if dob:
        fields["dob"] = dob

    return FieldResult(fields=fields, warnings=warnings, validated=validated)


def _extract_address_proof(text: str) -> FieldResult:
    fields: dict[str, str] = {}
    warnings: list[str] = []

    pincode = _first(_PINCODE_RE, text)
    if pincode:
        fields["pincode"] = pincode
        validated = True
    else:
        warnings.append("No 6-digit pincode found - please enter the address manually.")
        validated = False

    # Layouts vary far too much to parse reliably. The one dependable anchor is
    # the pincode: on a bill or letter the address sits immediately above it, so
    # take that neighbourhood. Picking the longest lines instead just surfaces
    # whichever paragraph of printed boilerplate happens to be wordiest.
    block = _lines_around_pincode(_lines(text), pincode)
    if block:
        fields["address"] = ", ".join(block)
        warnings.append("Address is a best-effort read - please check it against the image.")

    return FieldResult(fields=fields, warnings=warnings, validated=validated)


# How many lines above the pincode to treat as the address block.
_ADDRESS_LINES_ABOVE = 2


def _lines_around_pincode(lines: list[str], pincode: str) -> list[str]:
    """The address block: the line carrying the pincode plus the ones just above.

    Returns nothing when the pincode wasn't found, rather than guessing - an
    empty field is easier for HR than a wrong one.
    """
    if not pincode:
        return []
    for index, line in enumerate(lines):
        if pincode in line:
            start = max(0, index - _ADDRESS_LINES_ABOVE)
            return lines[start : index + 1]
    return []


def _extract_education(text: str) -> FieldResult:
    fields: dict[str, str] = {}
    warnings: list[str] = []
    lines = _lines(text)

    year = _first(_YEAR_RE, text)
    if year and _YEAR_MIN <= int(year) <= _YEAR_MAX:
        fields["year"] = year
        validated = True
    else:
        warnings.append("No plausible year of passing found.")
        validated = False

    institution = _match_hint(lines, _INSTITUTION_PATTERNS)
    if institution:
        fields["institution"] = institution
    degree = _match_hint(lines, _DEGREE_PATTERNS)
    if degree:
        fields["degree"] = degree

    warnings.append("Certificate layouts vary - please check every field against the image.")
    return FieldResult(fields=fields, warnings=warnings, validated=validated)


def _match_hint(lines: list[str], patterns: tuple[re.Pattern[str], ...]) -> str:
    for line in lines:
        if any(pattern.search(line) for pattern in patterns):
            return line
    return ""


# Keyed on the casefolded label: `docType` is a free-form form field, so
# "Aadhaar Card" must not silently disable extraction.
_EXTRACTORS = {
    AADHAAR.casefold(): _extract_aadhaar,
    PAN.casefold(): _extract_pan,
    ADDRESS_PROOF.casefold(): _extract_address_proof,
    EDUCATION.casefold(): _extract_education,
}


def supports(doc_type: str) -> bool:
    """True when this document type has extraction rules."""
    return (doc_type or "").strip().casefold() in _EXTRACTORS


def extract_fields(doc_type: str, text: str) -> FieldResult:
    """Structured fields for a supported document type.

    Unsupported types and empty text yield an empty, unvalidated result rather
    than raising - an unparseable document must never block HR's review.
    """
    extractor = _EXTRACTORS.get((doc_type or "").strip().casefold())
    if extractor is None:
        return FieldResult(
            fields={},
            warnings=[f"No extraction rules for '{doc_type}' - enter values manually."],
            validated=False,
        )
    if not (text or "").strip():
        return FieldResult(
            fields={},
            warnings=["No text could be read from the image."],
            validated=False,
        )
    return extractor(text)
