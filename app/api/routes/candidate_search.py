"""Dedicated candidates search/pagination endpoint.

Registered BEFORE the generic `/api/{resource}` router (same pattern as
app/api/routes/candidate_delete.py and job_search.py) so
`GET /api/candidates/search` wins over the generic router's
`/api/{resource}/{item_id}` pattern treating "search" as a candidate id.

A dedicated endpoint, not the generic paginated route, because
CandidateListView.tsx's stage-status/CTC-band/keyword-band filters are
cross-resource/derived rather than stored fields - see
app/services/candidate_search.py and candidate_pipeline.py for why.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_user
from app.services.candidate_search import search_candidates

router = APIRouter(prefix="/api/candidates", tags=["candidates"], dependencies=[Depends(require_user)])


@router.get("/search")
def search(
    page: int = 1,
    limit: int = 15,
    search: str | None = None,
    department: str = "All",
    stageStatus: str = "All",
    source: str = "All",
    rejected: str = "all",
    maxNoticePeriod: int = 9999,
    minExperience: float = 0,
    ctcBand: str = "all",
    ctcMin: str | None = None,
    ctcMax: str | None = None,
    keywordBand: str = "all",
    jobId: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    result = search_candidates(
        session,
        page=page,
        limit=limit,
        search=search,
        department=department,
        stage_status=stageStatus,
        source=source,
        rejected_filter=rejected,
        max_notice_period=maxNoticePeriod,
        min_experience=minExperience,
        ctc_band=ctcBand,
        ctc_min=ctcMin,
        ctc_max=ctcMax,
        keyword_band=keywordBand,
        job_id=jobId,
    )
    return result.to_dict()
