"""Background job status/retry — generic across every job type.

Named `/api/background-jobs`, NOT `/api/jobs`: that path is already taken by
the generic resource router for job POSTINGS (recruitment listings). This is
a different, unrelated "job" (a queued background task) — see
app/repositories/job_repository.py.

HR-only (`require_user`): a job's payload/result can carry candidate data
(e.g. an OCR extraction result), so this is not public, same as every other
HR-only route in this app.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_job_repository, require_user
from app.core.errors import NotFoundError, ValidationError
from app.repositories.job_repository import JobRecord, JobRepository

router = APIRouter(
    prefix="/api/background-jobs", tags=["background-jobs"], dependencies=[Depends(require_user)]
)


def _to_dict(record: JobRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "jobType": record.job_type,
        "status": record.status,
        "attempts": record.attempts,
        "maxAttempts": record.max_attempts,
        "result": record.result,
        "error": record.error,
        "createdAt": record.created_at.isoformat() if record.created_at else None,
        "updatedAt": record.updated_at.isoformat() if record.updated_at else None,
        "startedAt": record.started_at.isoformat() if record.started_at else None,
        "completedAt": record.completed_at.isoformat() if record.completed_at else None,
    }


@router.get("/{job_id}")
def get_job(job_id: str, repo: JobRepository = Depends(get_job_repository)) -> dict[str, Any]:
    job = repo.get(job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")
    return _to_dict(job)


@router.post("/{job_id}/retry")
def retry_job(job_id: str, repo: JobRepository = Depends(get_job_repository)) -> dict[str, Any]:
    """Re-queue a failed (or cancelled) job immediately. Only valid from a
    terminal, non-completed state — a queued/processing/retrying job is
    already going to run again on its own."""
    job = repo.get(job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")
    if not repo.retry_now(job_id):
        raise ValidationError(
            f"Job is '{job.status}' - only a 'failed' or 'cancelled' job can be retried."
        )
    return _to_dict(repo.get(job_id))


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, repo: JobRepository = Depends(get_job_repository)) -> dict[str, Any]:
    """Cancel a job that hasn't started processing yet."""
    job = repo.get(job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")
    if not repo.cancel(job_id):
        raise ValidationError(f"Job is '{job.status}' - only a 'queued' or 'retrying' job can be cancelled.")
    return _to_dict(repo.get(job_id))
