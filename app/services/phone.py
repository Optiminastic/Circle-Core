"""Phone number normalisation for outbound calls.

Candidate phones are stored loosely: public applications save "+91 " plus 10
digits, HR-entered ones vary. Telephony providers need strict E.164, so this
turns any Indian mobile spelling into "+91XXXXXXXXXX" or refuses it.
"""

from __future__ import annotations

import re

_COUNTRY_CODE = "91"
_MOBILE_DIGITS = 10
# Indian mobile numbers start with 6, 7, 8 or 9; landlines and others do not.
_MOBILE_PATTERN = re.compile(r"[6-9]\d{9}")


class InvalidPhoneError(ValueError):
    """The value is not a callable Indian mobile number."""


def normalize_indian_mobile(raw: str) -> str:
    """Return the number as E.164 ("+919876543210") or raise InvalidPhoneError."""
    if not isinstance(raw, str):
        raise InvalidPhoneError("Phone number is missing")
    digits = re.sub(r"\D", "", raw)
    national = _national_part(digits)
    if not _MOBILE_PATTERN.fullmatch(national):
        raise InvalidPhoneError("Not an Indian mobile number")
    return f"+{_COUNTRY_CODE}{national}"


def _national_part(digits: str) -> str:
    """Strip a leading country code or trunk 0 from a digits-only string."""
    if len(digits) == _MOBILE_DIGITS + len(_COUNTRY_CODE) and digits.startswith(_COUNTRY_CODE):
        return digits[len(_COUNTRY_CODE) :]
    if len(digits) == _MOBILE_DIGITS + 1 and digits.startswith("0"):
        return digits[1:]
    return digits
