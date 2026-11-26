"""app.services.job_search — pure logic (parsing, matching, sorting) that
needs no database. The actual query (applicant_counts, search_jobs) needs a
real Postgres and is covered live (see the Phase 5 pagination-migration
verification notes in docs/PERFORMANCE_IMPLEMENTATION_LOG.md).
"""

from __future__ import annotations

from app.services.job_search import _matches_search, _sort_key, parse_salary_lpa


def test_parse_salary_lpa_matches_the_frontend_tonum_behavior() -> None:
    assert parse_salary_lpa("12 LPA") == 12.0
    assert parse_salary_lpa("12.5 LPA") == 12.5
    assert parse_salary_lpa("") == 0.0
    assert parse_salary_lpa(None) == 0.0
    assert parse_salary_lpa("not a number") == 0.0
    assert parse_salary_lpa("₹15,00,000") == 1500000.0  # commas stripped, digits kept


def test_matches_search_checks_title_department_location_and_id() -> None:
    job = {"title": "Backend Engineer", "department": "Engineering", "location": "Remote", "id": "JOB-42"}
    assert _matches_search(job, "")
    assert _matches_search(job, "backend")
    assert _matches_search(job, "engineering")
    assert _matches_search(job, "remote")
    assert _matches_search(job, "job-42")
    assert not _matches_search(job, "frontend")


def test_sort_key_title_is_case_insensitive() -> None:
    assert _sort_key({"title": "banana"}, "title", {}) < _sort_key({"title": "Cherry"}, "title", {})


def test_sort_key_exp_uses_the_stored_numeric_field() -> None:
    assert _sort_key({"minExperienceYears": 5}, "exp", {}) == 5
    assert _sort_key({}, "exp", {}) == 0


def test_sort_key_salary_parses_free_text() -> None:
    assert _sort_key({"salaryMin": "20 LPA"}, "salary", {}) == 20.0


def test_sort_key_status_is_a_plain_string() -> None:
    assert _sort_key({"status": "Open"}, "status", {}) == "Open"


def test_sort_key_applicants_reads_the_counts_map_by_job_id() -> None:
    counts = {"JOB-1": 7, "JOB-2": 2}
    assert _sort_key({"id": "JOB-1"}, "applicants", counts) == 7
    assert _sort_key({"id": "JOB-unknown"}, "applicants", counts) == 0
