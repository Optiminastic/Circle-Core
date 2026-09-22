"""Employee code allocation — the one place an `EMP-####` code is minted.

Employee codes are the primary key of the `employees` table, so they must be
unique. Uniqueness comes from a Postgres sequence (see `_EMPLOYEE_CODE_SEQ_DDL`
in `app.db.database`) rather than from checking-before-insert, which races.

This deliberately lives server-side. The previous scheme generated codes in the
browser with `randomId('EMP', 9000, 1000)`, so two HR users converting
candidates at the same time could mint the same code and silently overwrite an
existing employee record.

When the shared identity service takes over code allocation, this module is the
only thing that has to change: the route keeps its shape and callers keep theirs.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import RepositoryError
from app.core.logging import get_logger

logger = get_logger("curcle.employee_codes")

CODE_PREFIX = "EMP-"

# The sequence alone guarantees we never reissue a number. This extra existence
# check guards only against legacy rows whose id does not match the '^EMP-[0-9]+$'
# pattern the boot-time sync scans for, so it can never fast-forward past them.
_MAX_ATTEMPTS = 5


def allocate_employee_code(session: Session) -> str:
    """Return a fresh, unused employee code. Never returns a code already in use."""
    for _ in range(_MAX_ATTEMPTS):
        number = session.execute(text('SELECT nextval(\'employee_code_seq\')')).scalar_one()
        code = f"{CODE_PREFIX}{number}"
        taken = session.execute(
            text("SELECT 1 FROM employees WHERE id = :id LIMIT 1"), {"id": code}
        ).first()
        if taken is None:
            return code
        logger.warning("Employee code %s was already taken; drawing another.", code)
    raise RepositoryError(
        "Could not allocate a free employee code after several attempts. "
        "The employee_code_seq sequence is likely behind the existing data."
    )
