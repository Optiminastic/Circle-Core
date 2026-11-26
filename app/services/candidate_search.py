"""Candidates listing with search/filter/pagination — the dedicated
endpoint behind CandidateListView.tsx, mirroring its exact filter logic
(see components/CandidateListView.tsx lines ~280-360 in circle-fe) so
nothing silently behaves differently once the browser stops downloading
the full candidates/schedules/interviews/iqTests/testInvites/jobs tables
to compute this client-side. See
docs/PERFORMANCE_IMPLEMENTATION_LOG.md's pagination-migration entry and
app/services/candidate_pipeline.py (the stage-status port this depends on).

Like app/services/job_search.py, this computes in Python rather than SQL:
the stage-status/CTC-band/keyword-band filters are cross-resource/derived,
and the production table sizes measured during this program's own
Phase 4 audit (~100 rows) make a correct, testable Python port far lower
risk than a bespoke SQL reimplementation of this much conditional logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.candidate_pipeline import candidate_stage_status, context_for, group_pipeline_context_by_candidate

_CANDIDATES_TABLE = "candidates"
_SCHEDULES_TABLE = "schedules"
_INTERVIEWS_TABLE = "interviews"
_IQ_TESTS_TABLE = "iq_tests"
_TEST_INVITES_TABLE = "test_invites"
_JOBS_TABLE = "jobs"

CTC_BANDS = frozenset({"all", "lt5", "5to10", "10to20", "gte20", "custom"})
KEYWORD_BANDS = frozenset({"all", "high", "medium", "low", "none"})


def _as_dict(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


def _fetch_all(session: Session, table: str) -> list[dict[str, Any]]:
    rows = session.execute(text(f'SELECT data FROM "{table}"')).fetchall()
    return [_as_dict(row[0]) for row in rows]


def parse_ctc_lpa(value: Any) -> float | None:
    """Port of circle-fe/lib/utils.ts's parseCtcLpa(): CTC is free text
    ("2", "2.3", or occasionally the full annual rupee figure typed by
    mistake) - extract the first number, and normalize anything >= 1000
    down to LPA (no real LPA is ever that large). None if nothing numeric."""
    v = str(value or "").strip()
    if not v:
        return None
    match = re.search(r"[\d.]+", v)
    if not match:
        return None
    try:
        num = float(match.group(0))
    except ValueError:
        return None
    if num >= 1000:
        num = num / 100000
    return num


def _matches_ctc(candidate: dict[str, Any], band: str, ctc_min: str | None, ctc_max: str | None) -> bool:
    if band == "all":
        return True
    lpa = parse_ctc_lpa(candidate.get("expectedCtc"))
    if lpa is None:
        return False
    if band == "lt5":
        return lpa < 5
    if band == "5to10":
        return 5 <= lpa < 10
    if band == "10to20":
        return 10 <= lpa < 20
    if band == "gte20":
        return lpa >= 20
    # custom - either bound blank means "no lower/upper limit".
    lo = float("-inf") if not ctc_min else float(ctc_min)
    hi = float("inf") if not ctc_max else float(ctc_max)
    return lo <= lpa <= hi


def _matches_keywords(candidate: dict[str, Any], band: str, jobs_by_id: dict[str, dict[str, Any]]) -> bool:
    if band == "all":
        return True
    job = jobs_by_id.get(candidate.get("jobId") or "")
    total = len(((job or {}).get("keywords")) or [])
    matches = candidate.get("keywordMatches")
    if total == 0 or matches is None:
        return band == "none"
    ratio = len(matches) / total
    if band == "high":
        return ratio >= 0.7
    if band == "medium":
        return 0.4 <= ratio < 0.7
    if band == "low":
        return ratio < 0.4
    return False


def _hit_keywords(candidate: dict[str, Any], q: str) -> list[str]:
    if q == "":
        return []
    matches = candidate.get("keywordMatches") or []
    return [k for k in matches if q in k.lower()]


def _matches_search(candidate: dict[str, Any], q: str) -> bool:
    if q == "":
        return True
    if q in str(candidate.get("fullName") or "").lower():
        return True
    if q in str(candidate.get("appliedRole") or "").lower():
        return True
    resume_text = candidate.get("resumeText")
    if resume_text and q in resume_text.lower():
        return True
    return len(_hit_keywords(candidate, q)) > 0


def _recency_key(candidate: dict[str, Any]) -> str:
    return candidate.get("appliedAt") or candidate.get("appliedDate") or ""


@dataclass(frozen=True)
class CandidateSearchResult:
    data: list[dict[str, Any]]
    page: int
    limit: int
    total: int

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
        }


def search_candidates(
    session: Session,
    *,
    page: int = 1,
    limit: int = 15,
    search: str | None = None,
    department: str = "All",
    stage_status: str = "All",
    source: str = "All",
    rejected_filter: str = "all",
    max_notice_period: int = 9999,
    min_experience: float = 0,
    ctc_band: str = "all",
    ctc_min: str | None = None,
    ctc_max: str | None = None,
    keyword_band: str = "all",
    job_id: str | None = None,
) -> CandidateSearchResult:
    """`job_id` scopes the result to one job's applicants (the Applicants
    tab on a job posting) - matches `applicants = candidates.filter(c =>
    c.jobId === jobId)` in circle-fe's app/(dashboard)/jobs/[jobId]/
    applicants/page.tsx, just applied server-side instead of after
    downloading every candidate."""
    page = max(1, page)
    limit = max(1, min(limit, 200))
    ctc_band = ctc_band if ctc_band in CTC_BANDS else "all"
    keyword_band = keyword_band if keyword_band in KEYWORD_BANDS else "all"

    candidates = [c for c in _fetch_all(session, _CANDIDATES_TABLE) if c.get("status") != "Blacklisted"]
    if job_id:
        candidates = [c for c in candidates if c.get("jobId") == job_id]
    schedules = _fetch_all(session, _SCHEDULES_TABLE)
    interviews = _fetch_all(session, _INTERVIEWS_TABLE)
    iq_tests = _fetch_all(session, _IQ_TESTS_TABLE)
    invites = _fetch_all(session, _TEST_INVITES_TABLE)
    jobs_by_id = {j["id"]: j for j in _fetch_all(session, _JOBS_TABLE) if j.get("id")}

    pipeline_by_candidate = group_pipeline_context_by_candidate(schedules, interviews, iq_tests, invites)

    q = (search or "").strip().lower()
    matched: list[dict[str, Any]] = []
    for cand in candidates:
        stage = candidate_stage_status(cand, context_for(pipeline_by_candidate, cand.get("id", "")))

        is_rejected = cand.get("status") == "Rejected"
        matches_rejected = is_rejected if rejected_filter == "rejected" else True
        matches_status = rejected_filter == "rejected" or stage_status == "All" or stage == stage_status
        matches_dept = department == "All" or cand.get("department") == department
        matches_source = source == "All" or cand.get("sourceOfApplication") == source
        matches_notice = (cand.get("noticePeriodDays") or 0) <= max_notice_period
        matches_exp = (cand.get("totalExperienceYears") or 0) >= min_experience

        if not (
            matches_rejected
            and _matches_search(cand, q)
            and matches_dept
            and matches_status
            and matches_source
            and matches_notice
            and matches_exp
            and _matches_ctc(cand, ctc_band, ctc_min, ctc_max)
            and _matches_keywords(cand, keyword_band, jobs_by_id)
        ):
            continue

        enriched = dict(cand)
        enriched["stageStatus"] = stage
        # resumeText is only ever used above for search matching - it's never
        # rendered anywhere in the frontend (not even the candidate detail
        # page) and can be tens of KB per candidate, the single largest
        # contributor to this endpoint's payload size. Stripped from the
        # LIST response; the generic GET /api/candidates/{id} route (the
        # detail page's own fetch) is untouched and still returns it.
        enriched.pop("resumeText", None)
        matched.append(enriched)

    matched.sort(key=_recency_key, reverse=True)

    total = len(matched)
    start = (page - 1) * limit
    page_candidates = matched[start : start + limit]

    return CandidateSearchResult(data=page_candidates, page=page, limit=limit, total=total)
