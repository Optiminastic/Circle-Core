"""Generic CRUD router shared by every resource (thin controller).

One implementation serves all resources; the registry drives validation. Routes
delegate to the service and translate nothing — errors bubble to the global
handlers as structured responses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from app.api.dependencies import get_resource_service
from app.core.config import Settings, get_settings
from app.domain.registry import get_resource
from app.services.resource_service import ResourceService
from app.services.sessions import COOKIE_NAME, read_session

Document = dict[str, Any]

# The ONLY generic operations reachable without a dashboard login — the public
# careers site + the token-gated public pages (candidate test, onboarding docs)
# depend on them. The unguessable id/token in the URL is the credential.
# Everything else on /api/{resource} (all candidate/employee/interview
# reads+writes) requires a valid session. `auth-users` is blocked here entirely
# (managed only via /api/auth/*). The interviewer sheet uses /api/public/* routes.
#
# Public LIST — the careers page lists job openings (frontend filters to open
# ones). Read-only; job writes still require a session.
_PUBLIC_LIST = {"jobs"}
# Reads by id: public job detail (apply page) + unguessable-token reads
# (candidate test + onboarding-doc pages).
_PUBLIC_GET_BY_ID = {"jobs", "test-invites", "doc-requests", "joining-confirmations"}
# Writes: only the candidate's own onboarding bank details. Test-invite writes go
# through the write-once /api/public/test/* endpoints instead of an arbitrary PATCH.
_PUBLIC_PATCH_BY_ID = {"doc-requests"}


def _is_public_generic(method: str, resource: str, has_item_id: bool) -> bool:
    if method == "GET" and not has_item_id and resource in _PUBLIC_LIST:
        return True
    if method == "GET" and has_item_id and resource in _PUBLIC_GET_BY_ID:
        return True
    if method == "PATCH" and has_item_id and resource in _PUBLIC_PATCH_BY_ID:
        return True
    return False


def guard_resources(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Router-level auth gate: require a session unless the op is public-allowlisted."""
    parts = request.url.path.strip("/").split("/")  # ["api", <resource>, <item_id>?]
    resource = parts[1] if len(parts) > 1 else ""
    has_item_id = len(parts) > 2
    if resource == "auth-users":
        # Never expose accounts (or their password hashes) via the generic API.
        raise HTTPException(status_code=404, detail="Not found.")
    if _is_public_generic(request.method, resource, has_item_id):
        return
    if not read_session(settings, request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in.")


router = APIRouter(prefix="/api", tags=["resources"], dependencies=[Depends(guard_resources)])


@router.get("/{resource}")
def list_all(
    resource: str,
    limit: int | None = None,
    offset: int = 0,
    service: ResourceService = Depends(get_resource_service),
) -> list[Document]:
    # Pagination is opt-in: without `limit` the full list is returned (unchanged
    # behavior); pass `?limit=50&offset=100` to page through large resources.
    return service.list(get_resource(resource), limit=limit, offset=offset)


@router.get("/{resource}/{item_id}")
def get_one(resource: str, item_id: str, service: ResourceService = Depends(get_resource_service)) -> Document:
    return service.get(get_resource(resource), item_id)


@router.post("/{resource}", status_code=201)
def create(resource: str, payload: Document = Body(...), service: ResourceService = Depends(get_resource_service)) -> Document:
    return service.create(get_resource(resource), payload)


@router.put("/{resource}/{item_id}")
def replace(resource: str, item_id: str, payload: Document = Body(...), service: ResourceService = Depends(get_resource_service)) -> Document:
    return service.replace(get_resource(resource), item_id, payload)


@router.patch("/{resource}/{item_id}")
def patch(resource: str, item_id: str, changes: Document = Body(...), service: ResourceService = Depends(get_resource_service)) -> Document:
    return service.patch(get_resource(resource), item_id, changes)


@router.delete("/{resource}/{item_id}", status_code=204, response_class=Response)
def remove(resource: str, item_id: str, service: ResourceService = Depends(get_resource_service)) -> Response:
    service.delete(get_resource(resource), item_id)
    return Response(status_code=204)
