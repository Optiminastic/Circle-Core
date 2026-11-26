"""Dashboard aggregate queries — computed in SQL (COUNT/GROUP BY/conditional
aggregates), never by downloading full tables into Python. See
docs/PERFORMANCE_AUDIT.md P0-2/P0-3 and docs/PERFORMANCE_ROADMAP.md Phase B2.

Every number here replaces the equivalent `Array.filter(...).length` that
previously ran client-side (or in `document_repository.list()` + Python) over
a fully-downloaded table — see `circle-fe/components/DashboardView.tsx` for
the exact client-side math each query below reproduces. Column/table names
are fixed literals in this module, never built from request input.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import RepositoryError

T = TypeVar("T")

#: 6 months at a 30.44-day average month — matches the frontend's prior
#: client-side `(now - joiningDate) / (msPerDay * 30.44) < 6` exactly, so
#: switching to this query changes no employee's probation status.
_PROBATION_DAYS = 6 * 30.44

#: Guards every `::timestamptz` cast below against a missing/malformed date
#: string in older records — a cast failure must never 500 the dashboard.
_ISO_DATE_PREFIX = r"^\d{4}-\d{2}-\d{2}"


class StatsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _run(self, op: Callable[[], T]) -> T:
        try:
            return op()
        except OperationalError:
            self._session.rollback()
            return op()

    def jobs_overview(self) -> dict[str, int]:
        try:
            row = self._run(
                lambda: self._session.execute(
                    text(
                        """
                        SELECT
                          count(*) AS total,
                          count(*) FILTER (WHERE data->>'status' = 'Open') AS open_positions
                        FROM jobs
                        """
                    )
                ).fetchone()
            )
            return {"total": int(row.total), "open": int(row.open_positions)}
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to compute jobs overview") from exc

    def candidates_overview(self) -> dict[str, int]:
        try:
            row = self._run(
                lambda: self._session.execute(
                    text(
                        """
                        SELECT
                          count(*) FILTER (
                            WHERE data->>'status' NOT IN ('Rejected', 'Duplicate Profile')
                          ) AS active
                        FROM candidates
                        """
                    )
                ).fetchone()
            )
            return {"active": int(row.active)}
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to compute candidates overview") from exc

    def interviewing_candidate_count(self) -> int:
        """Distinct candidates with at least one Scheduled interview."""
        try:
            row = self._run(
                lambda: self._session.execute(
                    text(
                        """
                        SELECT count(DISTINCT data->>'candidateId') AS n
                        FROM interviews
                        WHERE data->>'status' = 'Scheduled'
                        """
                    )
                ).fetchone()
            )
            return int(row.n)
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to compute interviewing candidate count") from exc

    def employees_overview(self) -> dict[str, int]:
        try:
            row = self._run(
                lambda: self._session.execute(
                    text(
                        f"""
                        SELECT
                          count(*) AS total,
                          count(*) FILTER (WHERE data->>'status' = 'Active') AS active,
                          count(*) FILTER (
                            WHERE data->>'status' = 'Active'
                              AND (data->>'joiningDate') ~ :iso_prefix
                              AND (data->>'joiningDate')::timestamptz <= now()
                              AND (data->>'joiningDate')::timestamptz > now() - interval '{_PROBATION_DAYS} days'
                          ) AS on_probation
                        FROM employees
                        """
                    ),
                    {"iso_prefix": _ISO_DATE_PREFIX},
                ).fetchone()
            )
            return {
                "total": int(row.total),
                "active": int(row.active),
                "onProbation": int(row.on_probation),
            }
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to compute employees overview") from exc

    def offboarding_overview(self) -> dict[str, int]:
        try:
            row = self._run(
                lambda: self._session.execute(
                    text(
                        """
                        SELECT
                          count(*) FILTER (WHERE data->>'status' != 'Completed') AS active_exits,
                          count(*) FILTER (WHERE data->>'status' = 'Notice Period Active') AS on_notice
                        FROM offboarding
                        """
                    )
                ).fetchone()
            )
            return {"activeExits": int(row.active_exits), "onNotice": int(row.on_notice)}
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to compute offboarding overview") from exc

    def upcoming_interviews(self, limit: int) -> list[dict[str, Any]]:
        """The next `limit` Scheduled interviews from now, soonest first, each
        paired with its candidate's CURRENT record (not the interview's own
        name/role/department snapshot) — joined here in SQL instead of the
        frontend downloading both full tables to do the same join client-side.
        A candidate who no longer exists (deleted) is paired with `None`."""
        try:
            rows = self._run(
                lambda: self._session.execute(
                    text(
                        """
                        SELECT i.data AS interview, c.data AS candidate
                        FROM interviews i
                        LEFT JOIN candidates c ON c.id = (i.data->>'candidateId')
                        WHERE i.data->>'status' = 'Scheduled'
                          AND (i.data->>'dateTime') ~ :iso_prefix
                          AND (i.data->>'dateTime')::timestamptz >= now()
                        ORDER BY (i.data->>'dateTime')::timestamptz ASC
                        LIMIT :limit
                        """
                    ),
                    {"iso_prefix": _ISO_DATE_PREFIX, "limit": limit},
                ).fetchall()
            )
            return [{"interview": row.interview, "candidate": row.candidate} for row in rows]
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to compute upcoming interviews") from exc
