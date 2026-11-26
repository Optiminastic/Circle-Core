"""Dedicated job-postings search/pagination endpoint.

Registered BEFORE the generic `/api/{resource}` router (same pattern as
app/api/routes/candidate_delete.py) so `GET /api/jobs/search` wins over the
generic router's `/api/{resource}/{item_id}` pattern treating "search" as
an item id.

A dedicated endpoint, not the generic paginated route, because two of
JobListView.tsx's sort options (applicant count, parsed salary) are derived
rather than stored fields - see app/services/job_search.py for why.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_user
from app.services.job_search import search_jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_user)])


@router.get("/search")
def search(
    page: int = 1,
    limit: int = 15,
    search: str | None = None,
    sortBy: str | None = None,
    sortOrder: str = "asc",
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    result = search_jobs(
        session,
        page=page,
        limit=limit,
        search=search,
        sort_by=sortBy or "title",
        sort_order=sortOrder,
    )
    return result.to_dict()
