"""Pure-logic pieces of app.services.candidate_search — parsing, the
search/CTC/keyword predicates — that need no database. The full
search_candidates() query is covered live (see
docs/PERFORMANCE_IMPLEMENTATION_LOG.md's pagination-migration entry).
"""

from __future__ import annotations

from app.services.candidate_search import (
    _hit_keywords,
    _matches_ctc,
    _matches_keywords,
    _matches_search,
    parse_ctc_lpa,
)


def test_parse_ctc_lpa_matches_the_frontend_exactly() -> None:
    assert parse_ctc_lpa("5") == 5.0
    assert parse_ctc_lpa("5.5") == 5.5
    assert parse_ctc_lpa("") is None
    assert parse_ctc_lpa(None) is None
    assert parse_ctc_lpa("no numbers here") is None
    # A candidate who typed the full annual rupee figure instead of LPA.
    assert parse_ctc_lpa("500000") == 5.0


def test_ctc_band_all_matches_everything() -> None:
    assert _matches_ctc({"expectedCtc": "anything"}, "all", None, None)
    assert _matches_ctc({}, "all", None, None)


def test_ctc_band_boundaries() -> None:
    assert _matches_ctc({"expectedCtc": "4.9"}, "lt5", None, None)
    assert not _matches_ctc({"expectedCtc": "5"}, "lt5", None, None)
    assert _matches_ctc({"expectedCtc": "5"}, "5to10", None, None)
    assert _matches_ctc({"expectedCtc": "9.9"}, "5to10", None, None)
    assert not _matches_ctc({"expectedCtc": "10"}, "5to10", None, None)
    assert _matches_ctc({"expectedCtc": "10"}, "10to20", None, None)
    assert not _matches_ctc({"expectedCtc": "20"}, "10to20", None, None)
    assert _matches_ctc({"expectedCtc": "20"}, "gte20", None, None)
    assert _matches_ctc({"expectedCtc": "100"}, "gte20", None, None)


def test_ctc_band_excludes_candidates_with_no_parseable_ctc() -> None:
    assert not _matches_ctc({"expectedCtc": ""}, "lt5", None, None)
    assert not _matches_ctc({}, "gte20", None, None)


def test_ctc_custom_range_with_both_bounds() -> None:
    assert _matches_ctc({"expectedCtc": "12"}, "custom", "10", "15")
    assert not _matches_ctc({"expectedCtc": "9"}, "custom", "10", "15")
    assert not _matches_ctc({"expectedCtc": "16"}, "custom", "10", "15")


def test_ctc_custom_range_with_a_blank_bound_means_unbounded() -> None:
    assert _matches_ctc({"expectedCtc": "1"}, "custom", "", "15")  # no lower limit
    assert _matches_ctc({"expectedCtc": "50"}, "custom", "10", "")  # no upper limit


def test_keyword_band_all_matches_everything() -> None:
    assert _matches_keywords({}, "all", {})


def test_keyword_band_none_when_job_has_no_keywords_configured() -> None:
    job = {"id": "J1", "keywords": []}
    cand = {"jobId": "J1", "keywordMatches": []}
    assert _matches_keywords(cand, "none", {"J1": job})
    assert not _matches_keywords(cand, "high", {"J1": job})


def test_keyword_band_none_when_matches_were_never_computed() -> None:
    job = {"id": "J1", "keywords": ["python", "react"]}
    cand = {"jobId": "J1"}  # keywordMatches is undefined/absent
    assert _matches_keywords(cand, "none", {"J1": job})


def test_keyword_band_thresholds() -> None:
    job = {"id": "J1", "keywords": ["a", "b", "c", "d", "e"]}  # 5 keywords
    high = {"jobId": "J1", "keywordMatches": ["a", "b", "c", "d"]}  # 4/5 = 0.8
    medium = {"jobId": "J1", "keywordMatches": ["a", "b"]}  # 2/5 = 0.4
    low = {"jobId": "J1", "keywordMatches": ["a"]}  # 1/5 = 0.2
    jobs = {"J1": job}
    assert _matches_keywords(high, "high", jobs) and not _matches_keywords(high, "medium", jobs)
    assert _matches_keywords(medium, "medium", jobs) and not _matches_keywords(medium, "high", jobs)
    assert _matches_keywords(low, "low", jobs) and not _matches_keywords(low, "medium", jobs)


def test_hit_keywords_is_empty_when_the_query_is_empty() -> None:
    assert _hit_keywords({"keywordMatches": ["python"]}, "") == []


def test_hit_keywords_matches_case_insensitively() -> None:
    assert _hit_keywords({"keywordMatches": ["Python", "React"]}, "python") == ["Python"]


def test_matches_search_empty_query_matches_everyone() -> None:
    assert _matches_search({}, "")


def test_matches_search_checks_name_role_resume_and_keyword_matches() -> None:
    assert _matches_search({"fullName": "Jane Doe"}, "jane")
    assert _matches_search({"appliedRole": "Backend Engineer"}, "backend")
    assert _matches_search({"resumeText": "Experienced with Kubernetes"}, "kubernetes")
    assert _matches_search({"keywordMatches": ["graphql"]}, "graphql")
    assert not _matches_search({"fullName": "Jane Doe"}, "zzz-no-match")


def test_matches_search_handles_missing_optional_fields_gracefully() -> None:
    # No resumeText, no keywordMatches at all - must not raise.
    assert not _matches_search({"fullName": "A", "appliedRole": "B"}, "nomatch")
