"""Allocate employee codes (thin controller).

Mounted before the generic `/api/{resource}` router in `main.py`, so this exact
path wins over the catch-all — the same ordering trick the other specific
routers rely on.

Requires a dashboard session: minting an employee code is an HR action, not a
public one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_session, require_user
from app.core.logging import get_logger
from app.services.employee_codes import allocate_employee_code

router = APIRouter(prefix="/api/employee-codes", tags=["employees"], dependencies=[Depends(require_user)])

logger = get_logger("curcle.employee_codes")


@router.post("/allocate")
def allocate(session: Session = Depends(get_session)) -> dict[str, str]:
    code = allocate_employee_code(session)
    logger.info("Allocated employee code %s", code)
    return {"employeeCode": code}
