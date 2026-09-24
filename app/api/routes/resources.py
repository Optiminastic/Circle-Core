"""Generic CRUD router shared by every resource (thin controller).

One implementation serves all resources; the registry drives validation. Routes
delegate to the service and translate nothing — errors bubble to the global
handlers as structured responses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from app.api.dependencies import current_user, get_audit_service, get_resource_service
from app.core.config import Settings, get_settings
from app.domain.registry import get_resource
from app.services.audit_service import AuditService
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


# --- Audit trail for key generic writes ---------------------------------------
# Only a curated set of resources is audited, and only on create (a real
# milestone, not a routine edit) plus candidate status changes - so the trail
# reads as an HR "work report" instead of a firehose of every field edit.

# resource slug -> (action key, summary verb) for CREATE.
_CREATE_ACTIONS: dict[str, tuple[str, str]] = {
    "candidates": ("candidate.created", "Added candidate"),
    "employees": ("employee.created", "Onboarded employee"),
    "offboarding": ("offboarding.started", "Started offboarding for"),
}
_ENTITY_TYPE: dict[str, str] = {
    "candidates": "candidate",
    "employees": "employee",
    "offboarding": "employee",
    "schedules": "schedule",
}
# Human-friendly label, best-effort, from the common name fields on a document.
_LABEL_KEYS = ("fullName", "name", "candidateName", "employeeName", "title", "email")


def _label(doc: Document) -> str:
    for key in _LABEL_KEYS:
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(doc.get("id") or "record")


def _audit_create(audit: AuditService, user: dict[str, Any] | None, resource: str, doc: Document) -> None:
    if resource == "schedules":
        stype = doc.get("type") or "meeting"
        who = doc.get("candidateName") or doc.get("candidateId") or "a candidate"
        audit.record(
            actor=user,
            action="schedule.created",
            summary=f"Scheduled {stype} for {who}",
            entity_type="schedule",
            entity_id=doc.get("id"),
            entity_label=str(who),
            metadata={"type": stype},
        )
        return
    mapping = _CREATE_ACTIONS.get(resource)
    if not mapping:
        return
    action, verb = mapping
    label = _label(doc)
    audit.record(
        actor=user,
        action=action,
        summary=f"{verb} {label}",
        entity_type=_ENTITY_TYPE.get(resource),
        entity_id=doc.get("id") or doc.get("employeeId"),
        entity_label=label,
    )


def _audit_patch(
    audit: AuditService,
    user: dict[str, Any] | None,
    resource: str,
    item_id: str,
    changes: Document,
    doc: Document,
) -> None:
    # Only candidate status transitions are meaningful "key actions"; every other
    # patch is a routine edit and deliberately not logged.
    if resource != "candidates" or "status" not in changes:
        return
    status = changes.get("status")
    label = _label(doc)
    if status == "Rejected":
        audit.record(
            actor=user,
            action="candidate.rejected",
            summary=f"Rejected candidate {label}",
            entity_type="candidate",
            entity_id=item_id,
            entity_label=label,
        )
    else:
        audit.record(
            actor=user,
            action="candidate.stage_changed",
            summary=f"Moved candidate {label} to {status}",
            entity_type="candidate",
            entity_id=item_id,
            entity_label=label,
            metadata={"status": status},
        )


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
def create(
    resource: str,
    payload: Document = Body(...),
    service: ResourceService = Depends(get_resource_service),
    user: dict[str, Any] | None = Depends(current_user),
    audit: AuditService = Depends(get_audit_service),
) -> Document:
    created = service.create(get_resource(resource), payload)
    _audit_create(audit, user, resource, created)
    return created


@router.put("/{resource}/{item_id}")
def replace(resource: str, item_id: str, payload: Document = Body(...), service: ResourceService = Depends(get_resource_service)) -> Document:
    return service.replace(get_resource(resource), item_id, payload)


@router.patch("/{resource}/{item_id}")
def patch(
    resource: str,
    item_id: str,
    changes: Document = Body(...),
    service: ResourceService = Depends(get_resource_service),
    user: dict[str, Any] | None = Depends(current_user),
    audit: AuditService = Depends(get_audit_service),
) -> Document:
    updated = service.patch(get_resource(resource), item_id, changes)
    _audit_patch(audit, user, resource, item_id, changes, updated)
    return updated


@router.delete("/{resource}/{item_id}", status_code=204, response_class=Response)
def remove(resource: str, item_id: str, service: ResourceService = Depends(get_resource_service)) -> Response:
    service.delete(get_resource(resource), item_id)
    return Response(status_code=204)
