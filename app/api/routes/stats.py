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
from app.repositories.stats_repository import StatsRepository

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_user)])


@router.get("/overview")
def overview(repo: StatsRepository = Depends(get_stats_repository)) -> dict[str, Any]:
    """The 4 KPI cards on the dashboard. Each number was previously an
    `Array.filter(...).length` over a fully-downloaded table; every one is
    now a single COUNT/conditional-aggregate query — see
    StatsRepository for the exact SQL."""
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
        "offboarding": {"activeExits": offboarding["activeExits"], "onNotice": offboarding["onNotice"]},
    }


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
