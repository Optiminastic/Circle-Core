"""Per-resource search/sort configuration for the paginated `GET /api/{resource}`.

Kept separate from `registry.py` (Open/Closed, SRP) — most resources need no
entry here at all; `created_at` sorting (the pre-existing default ordering)
and no search are always available for every resource regardless of whether
it appears below. This only configures the handful of resources Phase 2
wires up real server-side pagination for (the ones PERFORMANCE_AUDIT.md P0-2
identified as large/hot: candidates, employees, jobs, interviews,
doc-requests) — "not every resource supports the same search semantics," so
nothing here is inferred generically from the data shape.
"""

from __future__ import annotations

#: Resource slug -> JSONB fields `search=` matches against (ILIKE, OR'd).
SEARCH_FIELDS: dict[str, tuple[str, ...]] = {
    "candidates": ("fullName", "email", "phone", "appliedRole", "currentCompany"),
    "employees": ("fullName", "email", "department", "designation"),
    "jobs": ("title", "department", "location"),
    "interviews": ("candidateName", "appliedRole"),
    "doc-requests": ("candidateName", "email"),
}

#: Resource slug -> extra fields `sortBy=` may select beyond the default
#: `createdAt` (always allowed for every resource, including ones with no
#: entry here at all).
SORTABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "candidates": ("fullName", "appliedDate", "status"),
    "employees": ("fullName", "department"),
    "jobs": ("title", "status"),
    "interviews": ("scheduledAt",),
    "doc-requests": ("status", "expiresAt"),
}

DEFAULT_SORT_FIELD = "createdAt"

#: Resources with real pagination wired up end to end (backend query +
#: frontend consumer). `GET /api/{resource}?page=` works for any resource
#: (the generic paginated path doesn't require an entry here), but these are
#: the ones this phase's audit/roadmap specifically targeted; everything
#: else keeps the original unpaginated default response shape for now,
#: documented as a deliberate scoping decision in PERFORMANCE_IMPLEMENTATION_LOG.md
#: rather than a silent gap.
PAGINATED_RESOURCES: frozenset[str] = frozenset(SEARCH_FIELDS) | frozenset(SORTABLE_FIELDS)


def search_fields_for(resource_slug: str) -> tuple[str, ...]:
    return SEARCH_FIELDS.get(resource_slug, ())


def sortable_fields_for(resource_slug: str) -> frozenset[str]:
    return frozenset({DEFAULT_SORT_FIELD, *SORTABLE_FIELDS.get(resource_slug, ())})
