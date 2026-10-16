"""Finding the BGV report link in what OnGrid sends back.

HR could see a finished report in OnGrid's own download menu and had no way to
get it from Circle. The status endpoint still answered 200 with
`reportUrl: null`, the button is rendered only when that field is set, so it
simply was not there - and the call that fetches it swallowed its error without
logging, so nothing anywhere said why.

This pins the extraction down as a pure function, so a response shape we do not
recognise is reported rather than quietly becoming "no report".
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.routes.bgv_ongrid import report_complete, report_url_from


def test_the_documented_field_is_used() -> None:
    assert report_url_from({"servingUrl": "https://ongrid.test/r.pdf"}) == "https://ongrid.test/r.pdf"


def test_the_status_payload_field_is_used() -> None:
    # The status call sometimes carries the link already, saving a second call.
    assert (
        report_url_from({"consolidatedReportUrl": "https://ongrid.test/c.pdf"})
        == "https://ongrid.test/c.pdf"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"servingUrl": "https://ongrid.test/r.pdf"}},
        {"result": {"consolidatedReportUrl": "https://ongrid.test/r.pdf"}},
    ],
)
def test_a_wrapped_response_is_still_read(payload: dict[str, Any]) -> None:
    # OnGrid wraps some responses in `data`; a link one level down is still a
    # link, and must not be read as "no report".
    assert report_url_from(payload) == "https://ongrid.test/r.pdf"


@pytest.mark.parametrize("payload", [{}, None, {"servingUrl": ""}, {"servingUrl": "   "}])
def test_nothing_usable_is_none(payload: dict[str, Any] | None) -> None:
    assert report_url_from(payload) is None


def test_a_non_url_value_is_not_a_report() -> None:
    # Guards against handing the UI something that cannot be opened: a status
    # word in the field would otherwise render as a live download button.
    assert report_url_from({"servingUrl": "PENDING"}) is None


def test_the_first_usable_link_wins_over_an_empty_one() -> None:
    assert (
        report_url_from({"servingUrl": "", "consolidatedReportUrl": "https://ongrid.test/c.pdf"})
        == "https://ongrid.test/c.pdf"
    )


# -- the real response -------------------------------------------------------


REAL_RESPONSE = {
    "url": "https://s3.ap-south-1.amazonaws.com/reports-prod-ongrid/187363/21269658/35b03a3e.pdf",
    "bgvChecksCompleted": False,
}


def test_ongrids_actual_report_response_is_read() -> None:
    # Captured from production. The link is under `url` - NOT `servingUrl`,
    # which is the only field the original code looked at, which is why a
    # perfectly good report showed up in Circle as "no report".
    assert report_url_from(REAL_RESPONSE) == REAL_RESPONSE["url"]


def test_a_report_is_offered_before_every_check_has_finished() -> None:
    # `bgvChecksCompleted: false` and a link in the same breath: OnGrid serves
    # an interim report while checks are still running. Waiting for all of them
    # would withhold a document HR can already use.
    assert report_url_from(REAL_RESPONSE) is not None
    assert report_complete(REAL_RESPONSE) is False


def test_a_finished_report_says_so() -> None:
    assert report_complete({"url": "https://x/r.pdf", "bgvChecksCompleted": True}) is True


def test_completeness_is_unknown_when_not_stated() -> None:
    # Never guess "final" - an interim report filed as the final one is worse
    # than no report at all.
    assert report_complete({"url": "https://x/r.pdf"}) is None
    assert report_complete(None) is None
