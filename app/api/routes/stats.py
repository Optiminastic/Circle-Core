"""Dashboard aggregate endpoints — replaces `DashboardShell`'s full-table
prefetch burst for the numbers `DashboardView.tsx` actually renders. See
docs/PERFORMANCE_AUDIT.md P0-3 and docs/PERFORMANCE_ROADMAP.md Phase B2.

Only two endpoints: the one the brief's own instruction ("use only endpoints
justified by the actual dashboard") calls for, after reading exactly what
DashboardView.tsx derives from its props. No speculative /api/stats/bgv or
/api/stats/onboarding — the dashboard doesn't show those numbers today.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_stats_repository, require_user
from app.core.cache import dashboard_cache
from app.repositories.stats_repository import StatsRepository

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_user)])

#: Short TTL, not a long one: this is the actual cross-worker staleness bound
#: (see app/core/cache.py) since there's no shared cache store. 15s keeps the
#: dashboard fast under repeated loads/navigation without numbers visibly
#: lagging a create/update by more than a few seconds.
OVERVIEW_CACHE_TTL_SECONDS = 15
OVERVIEW_CACHE_KEY = "stats:overview"


@router.get("/overview")
def overview(repo: StatsRepository = Depends(get_stats_repository)) -> dict[str, Any]:
    """The 4 KPI cards on the dashboard. Each number was previously an
    `Array.filter(...).length` over a fully-downloaded table; every one is
    now a single COUNT/conditional-aggregate query — see
    StatsRepository for the exact SQL. Cached for
    OVERVIEW_CACHE_TTL_SECONDS (Phase 3); invalidated on candidate/employee/
    job writes via app/api/routes/resources.py and candidate_delete.py."""

    def compute() -> dict[str, Any]:
        jobs = repo.jobs_overview()
        candidates = repo.candidates_overview()
        employees = repo.employees_overview()
        offboarding = repo.offboarding_overview()
        interviewing = repo.interviewing_candidate_count()
        return {
            "jobs": {"total": jobs["total"], "open": jobs["open"]},
            "candidates": {"active": candidates["active"], "interviewing": interviewing},
            "employees": {
                "total": employees["total"],
                "active": employees["active"],
                "onProbation": employees["onProbation"],
            },
            "offboarding": {
                "activeExits": offboarding["activeExits"],
                "onNotice": offboarding["onNotice"],
            },
        }

    return dashboard_cache.get_or_set(OVERVIEW_CACHE_KEY, OVERVIEW_CACHE_TTL_SECONDS, compute)


@router.get("/upcoming-interviews")
def upcoming_interviews(
    limit: int = 6, repo: StatsRepository = Depends(get_stats_repository)
) -> list[dict[str, Any]]:
    """Next `limit` Scheduled interviews from now, each already paired with
    its candidate's current record (joined in SQL — see
    StatsRepository.upcoming_interviews) instead of the frontend downloading
    both full tables to do the same join client-side."""
    limit = max(1, min(limit, 50))
    return repo.upcoming_interviews(limit)
