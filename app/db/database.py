"""Database access boundary.

Wraps the SQLAlchemy engine so the rest of the app depends on a small, stable
surface (connect, session, healthcheck, ensure_tables) instead of SQLAlchemy
internals. Engine creation is lazy and fault-tolerant: a missing/unreachable
database does not crash the process — endpoints degrade to 503 instead.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import RepositoryError
from app.core.logging import get_logger

logger = get_logger("curcle.db")


_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS "{table}" (
    id         TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Indexes that keep reads cheap as tables grow:
#  - created_at: the default list ordering (avoids a full sort).
#  - GIN(jsonb_path_ops): indexed JSONB containment (`data @> '{...}'`) so
#    filtered lookups (e.g. documents by entityId) don't scan every row.
_INDEX_DDL = (
    'CREATE INDEX IF NOT EXISTS "ix_{table}_created_at" ON "{table}" (created_at)',
    'CREATE INDEX IF NOT EXISTS "ix_{table}_data_gin" ON "{table}" USING GIN (data jsonb_path_ops)',
)

# Employee codes ("EMP-1042") are the primary key of the `employees` table, so they
# must be unique. A sequence makes that true by construction; the previous
# client-side `randomId('EMP', 9000, 1000)` picked a random number in 1000-9999 with
# no uniqueness check, which silently overwrites an existing employee on collision
# (~8% likely at 40 staff, a coin flip by ~110).
_EMPLOYEE_CODE_SEQ = "employee_code_seq"
_EMPLOYEE_CODE_SEQ_DDL = f'CREATE SEQUENCE IF NOT EXISTS "{_EMPLOYEE_CODE_SEQ}" AS bigint MINVALUE 1000 START 1000'

# Advance the sequence past any code already issued by the old random scheme, so a
# freshly created sequence cannot hand out a number that is already in use.
# GREATEST(...) means this only ever moves forward — re-running it can never rewind
# the sequence and reissue codes. Safe to run on every boot.
_EMPLOYEE_CODE_SEQ_SYNC = f"""
SELECT setval(
    '{_EMPLOYEE_CODE_SEQ}',
    GREATEST(
        (SELECT last_value FROM "{_EMPLOYEE_CODE_SEQ}"),
        COALESCE((SELECT MAX(substring(id from 5)::bigint)
                    FROM employees
                   WHERE id ~ '^EMP-[0-9]+$'), 0),
        1000
    ),
    true
)
"""


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    def connect(self) -> None:
        if not self._settings.has_database:
            logger.warning("DATABASE_URL is not set — the API will start but data endpoints return 503.")
            return
        try:
            # Latency tuning for a remote (e.g. Neon) database where every round
            # trip costs real wall-clock time:
            #  - AUTOCOMMIT: every repository operation is a single statement, so
            #    skipping the implicit BEGIN + COMMIT/ROLLBACK saves 2 RTTs/request.
            #  - pool_recycle instead of pool_pre_ping: pre_ping costs a SELECT 1
            #    RTT on every checkout; recycling connections before the server's
            #    idle timeout achieves the same safety for free.
            #  - TCP keepalives keep pooled connections alive through NAT/idle.
            self._engine = create_engine(
                self._settings.sqlalchemy_url,
                isolation_level="AUTOCOMMIT",
                pool_pre_ping=False,
                pool_recycle=self._settings.db_pool_recycle,
                pool_size=self._settings.db_pool_size,
                max_overflow=self._settings.db_max_overflow,
                pool_timeout=self._settings.db_pool_timeout,
                connect_args={
                    "connect_timeout": 10,
                    "keepalives": 1,
                    "keepalives_idle": 30,
                    "keepalives_interval": 10,
                    "keepalives_count": 3,
                },
                future=True,
            )
            self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)
            logger.info("Database engine initialized.")
        except SQLAlchemyError as exc:  # pragma: no cover - defensive
            logger.exception("Failed to initialize database engine: %s", exc)
            self._engine = None
            self._session_factory = None

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    @property
    def is_ready(self) -> bool:
        return self._session_factory is not None

    def session(self) -> Session:
        if self._session_factory is None:
            raise RepositoryError("Database is not configured. Set DATABASE_URL and restart.")
        return self._session_factory()

    def healthcheck(self) -> bool:
        if self._engine is None:
            return False
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            logger.error("Healthcheck failed: %s", exc)
            return False

    def ensure_tables(self, tables: list[str]) -> None:
        if self._engine is None:
            return
        try:
            with self._engine.begin() as conn:
                for table in tables:
                    conn.execute(text(_TABLE_DDL.format(table=table)))
                    for index_ddl in _INDEX_DDL:
                        conn.execute(text(index_ddl.format(table=table)))
            logger.info("Ensured %d resource tables (with indexes) exist.", len(tables))
        except SQLAlchemyError as exc:
            logger.exception("Failed to ensure tables: %s", exc)

    def ensure_employee_code_sequence(self) -> None:
        """Create the employee-code sequence and fast-forward it past existing codes.

        Must run after ensure_tables — the sync statement reads the `employees`
        table. A failure here is logged, not raised: the app still serves reads,
        and allocation surfaces the problem at the point of use instead.
        """
        if self._engine is None:
            return
        try:
            with self._engine.begin() as conn:
                conn.execute(text(_EMPLOYEE_CODE_SEQ_DDL))
                last = conn.execute(text(_EMPLOYEE_CODE_SEQ_SYNC)).scalar_one()
            logger.info("Employee code sequence ready (next code will be EMP-%d).", last + 1)
        except SQLAlchemyError as exc:
            logger.exception("Failed to ensure employee code sequence: %s", exc)
