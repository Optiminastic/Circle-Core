"""Job-postings listing with search/sort/pagination, including two sort
keys the generic resource pagination route can't express because they're
derived rather than stored fields:

- `applicants`: how many candidates have `jobId` set to this job - a COUNT
  grouped by job, not a column on the job record itself.
- `salary`: `salaryMin` is free text ("12 LPA"), not a number - sorting it
  needs the same digit-extraction `circle-fe/components/JobListView.tsx`'s
  own `toNum()` helper does, ported here so the two never disagree.

The jobs table is small (see docs/PERFORMANCE_IMPLEMENTATION_LOG.md's
production audit), so this fetches it in full and does search/sort/
pagination in Python rather than attempting a bespoke JOIN+regex SQL query
for what is, today, a handful of rows - the actual goal (the BROWSER never
downloading the full table, and never downloading the full candidates
table just to count applicants per job) is fully met either way.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

_JOBS_TABLE = "jobs"
_CANDIDATES_TABLE = "candidates"

#: Mirrors JobListView.tsx's `sort.key` options exactly.
SORTABLE_FIELDS = frozenset({"title", "exp", "salary", "status", "applicants"})
DEFAULT_SORT_FIELD = "title"


def _as_dict(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


def parse_salary_lpa(value: Any) -> float:
    """Port of JobListView.tsx's `toNum()`: strip everything but digits/dot,
    0 if nothing numeric remains. Kept as a free function so a test can
    pin its behavior against the TS reference independently of the query."""
    digits = re.sub(r"[^0-9.]", "", str(value or ""))
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


def applicant_counts(session: Session) -> dict[str, int]:
    """Job id -> number of candidates with that `jobId` - one aggregate
    query, never the full candidates table."""
    rows = session.execute(
        text(
            f"""
            SELECT data->>'jobId' AS job_id, count(*) AS n
              FROM "{_CANDIDATES_TABLE}"
             WHERE data->>'jobId' IS NOT NULL AND data->>'jobId' != ''
             GROUP BY data->>'jobId'
            """
        )
    ).fetchall()
    return {row[0]: int(row[1]) for row in rows}


@dataclass(frozen=True)
class JobSearchResult:
    data: list[dict[str, Any]]
    page: int
    limit: int
    total: int
    #: Computed from EVERY job, ignoring search/page - JobListView.tsx's
    #: summary cards ("Total openings", "Open", "Closed", total applicants)
    #: are global counts, not "however many matched this search/page".
    open_count: int
    closed_count: int
    total_jobs: int
    total_applicants: int

    def to_dict(self) -> dict[str, Any]:
        total_pages = max(1, -(-self.total // self.limit)) if self.total else 0
        return {
            "data": self.data,
            "pagination": {
                "page": self.page,
                "limit": self.limit,
                "total": self.total,
                "totalPages": total_pages,
                "hasNextPage": self.page < total_pages,
                "hasPreviousPage": self.page > 1,
            },
            "summary": {
                "totalJobs": self.total_jobs,
                "openCount": self.open_count,
                "closedCount": self.closed_count,
                "totalApplicants": self.total_applicants,
            },
        }


def _matches_search(job: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    haystacks = (job.get("title"), job.get("department"), job.get("location"), job.get("id"))
    return any(q in str(h or "").lower() for h in haystacks)


def _sort_key(job: dict[str, Any], sort_by: str, counts: dict[str, int]):
    if sort_by == "exp":
        return job.get("minExperienceYears") or 0
    if sort_by == "salary":
        return parse_salary_lpa(job.get("salaryMin"))
    if sort_by == "status":
        return str(job.get("status") or "")
    if sort_by == "applicants":
        return counts.get(job.get("id"), 0)
    return str(job.get("title") or "").lower()


def search_jobs(
    session: Session,
    *,
    page: int = 1,
    limit: int = 15,
    search: str | None = None,
    sort_by: str = DEFAULT_SORT_FIELD,
    sort_order: str = "asc",
) -> JobSearchResult:
    page = max(1, page)
    limit = max(1, min(limit, 200))
    sort_by = sort_by if sort_by in SORTABLE_FIELDS else DEFAULT_SORT_FIELD
    sort_desc = str(sort_order).lower() == "desc"

    rows = session.execute(text(f'SELECT data FROM "{_JOBS_TABLE}"')).fetchall()
    all_jobs = [_as_dict(row[0]) for row in rows]
    counts = applicant_counts(session)

    # Global summary, from every job - unaffected by the search filter/page
    # applied below.
    open_count = sum(1 for j in all_jobs if j.get("status") == "Open")
    closed_count = sum(1 for j in all_jobs if j.get("status") in ("Closed", "On Hold"))
    total_applicants = sum(counts.values())

    q = (search or "").strip().lower()
    jobs = [j for j in all_jobs if _matches_search(j, q)]
    jobs.sort(key=lambda j: _sort_key(j, sort_by, counts), reverse=sort_desc)

    total = len(jobs)
    start = (page - 1) * limit
    page_jobs = jobs[start : start + limit]
    # Injected so the frontend never needs the full candidates table just to
    # show "N applicants" on a row it's already rendering.
    for job in page_jobs:
        job["applicantCount"] = counts.get(job.get("id"), 0)

    return JobSearchResult(
        data=page_jobs,
        page=page,
        limit=limit,
        total=total,
        open_count=open_count,
        closed_count=closed_count,
        total_jobs=len(all_jobs),
        total_applicants=total_applicants,
    )
