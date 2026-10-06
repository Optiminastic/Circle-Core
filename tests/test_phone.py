import pytest

from app.services.phone import InvalidPhoneError, normalize_indian_mobile


@pytest.mark.parametrize(
    "raw",
    ["+91 9876543210", "9876543210", "09876543210", "+919876543210", "91 98765-43210", " +91 98765 43210 "],
)
def test_accepts_indian_mobile_formats(raw: str) -> None:
    assert normalize_indian_mobile(raw) == "+919876543210"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "12345",  # too short
        "5876543210",  # Indian mobiles start with 6-9
        "+1 415 555 0100",  # foreign
        "022 2345 6789",  # landline
        "98765432101",  # 11 digits, no valid prefix
        None,
    ],
)
def test_rejects_non_mobile_numbers(raw: object) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_indian_mobile(raw)  # type: ignore[arg-type]
