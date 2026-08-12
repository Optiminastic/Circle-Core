"""Move a candidate's documents into the employee's S3 folder on conversion.

When an onboarding candidate becomes an employee, their resume and joining
documents (stored under `documents/candidate/{candidateId}/...`) must move to
`documents/employee/{employeeId}/...` so the new employee's Documents tab
(which lists by entityType+entityId) actually shows them, and so a later
cleanup-delete of the stale candidate record can't sweep up documents that now
belong to a real employee.

Document rows are looked up by their own `id` everywhere else in the app
(`doc_requests.submissions[].documentId`, an employee's `avatarUrl` built from
`documentPreviewUrl(documentId)`) — never by entityType/entityId or storageKey.
So migrating a document only requires updating its own row's entityType,
entityId, and storageKey (plus moving the underlying blob); every existing
reference keeps working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_repository, get_storage, require_user
from app.api.routes.documents import _safe_name
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.storage.base import FileStorage

router = APIRouter(prefix="/api/candidates", tags=["candidates"], dependencies=[Depends(require_user)])

logger = get_logger("curcle.candidate_promote")


@router.post("/{candidate_id}/promote-documents/{employee_id}")
def promote_candidate_documents(
    candidate_id: str,
    employee_id: str,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> dict[str, int]:
    migrated = 0
    for doc in repo.find("documents", {"entityType": "candidate", "entityId": candidate_id}):
        old_key = doc.get("storageKey")
        if not old_key:
            continue
        try:
            data, content_type = storage.get(old_key)
        except Exception:  # noqa: BLE001 — a missing/unreadable blob must not block promotion
            logger.warning("Could not read blob for document %s during promotion", doc.get("id"))
            continue
        new_key = f"documents/employee/{employee_id}/{doc['id']}_{_safe_name(doc.get('fileName') or 'file')}"
        storage.put(new_key, data, content_type or doc.get("contentType") or "application/octet-stream")
        try:
            storage.delete(old_key)
        except Exception:  # noqa: BLE001 — best-effort cleanup of the old blob
            logger.warning("Could not delete old blob %s during promotion", old_key)
        doc["entityType"] = "employee"
        doc["entityId"] = employee_id
        doc["storageKey"] = new_key
        repo.upsert("documents", doc["id"], doc)
        migrated += 1
    logger.info(
        "Promoted %d document(s) from candidate %s to employee %s", migrated, candidate_id, employee_id
    )
    return {"migrated": migrated}
