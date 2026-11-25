"""Phase-0 performance observability: request timing + SQLAlchemy instrumentation.

Lightweight, dependency-free (stdlib `logging` + SQLAlchemy's own event hooks).
Nothing here changes application behavior — it only measures and logs.

Safety:
  - Never logs request/response bodies, auth headers, or query parameter values
    (SQL statements logged are already parameterized placeholders, e.g. "%(id)s",
    never the literal values — those stay in `parameters`, which is never logged).
  - Paths are logged as their matched ROUTE TEMPLATE (e.g.
    "/api/doc-requests/{token}/upload"), never the raw path with real ids/tokens,
    to avoid both high-cardinality labels and leaking identifiers into logs.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.logging import get_logger

logger = get_logger("curcle.perf")

REQUEST_ID_HEADER = "X-Request-ID"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Configurable thresholds (env-driven, sane defaults — no restart-time config file).
SLOW_REQUEST_MS = _env_int("PERF_SLOW_REQUEST_MS", 1000)
SLOW_QUERY_MS = _env_int("PERF_SLOW_QUERY_MS", 200)
SLOW_CHECKOUT_MS = _env_int("PERF_SLOW_CHECKOUT_MS", 500)


@dataclass
class _PerfCounters:
    """In-process counters for the benchmark script / tests to read.

    Intentionally not exposed over HTTP: an unauthenticated stats endpoint on a
    system holding candidate/employee PII is its own review (see
    PERFORMANCE_ROADMAP.md Phase C2 for a real metrics endpoint/exporter)."""

    total_requests: int = 0
    slow_requests: int = 0
    total_queries: int = 0
    slow_queries: int = 0
    failed_queries: int = 0
    lock_free_note: str = field(
        default="Counters are best-effort under multi-worker/multi-thread concurrency; "
        "they're a debugging aid, not a metrics system.",
        repr=False,
    )


perf_counters = _PerfCounters()


# --------------------------------------------------------------------------- #
# HTTP request timing middleware
# --------------------------------------------------------------------------- #


def install_request_timing(app: FastAPI) -> None:
    """Registers an outermost timing middleware (added last = outermost in
    Starlette's middleware stack, so it measures the full request including
    CORS/GZip/rate-limit overhead, not just the route handler)."""

    @app.middleware("http")
    async def _request_timing(request: Request, call_next):  # noqa: ANN001
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            perf_counters.total_requests += 1
            logger.exception(
                "http_request method=%s path=%s status=ERROR duration_ms=%.1f request_id=%s",
                request.method,
                _route_template(request),
                duration_ms,
                request_id,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        perf_counters.total_requests += 1
        response.headers[REQUEST_ID_HEADER] = request_id

        route_path = _route_template(request)
        is_slow = duration_ms >= SLOW_REQUEST_MS
        if is_slow:
            perf_counters.slow_requests += 1

        log_fn = logger.warning if is_slow else logger.info
        log_fn(
            "http_request method=%s path=%s status=%d duration_ms=%.1f request_id=%s%s",
            request.method,
            route_path,
            response.status_code,
            duration_ms,
            request_id,
            " SLOW" if is_slow else "",
        )
        return response


def _route_template(request: Request) -> str:
    """The matched route's path TEMPLATE (e.g. '/api/candidates/{id}'), never the
    raw URL — keeps the metric low-cardinality and never logs a real id/token."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "<unmatched>"


# --------------------------------------------------------------------------- #
# SQLAlchemy instrumentation: query timing, failures, connection hold time
# --------------------------------------------------------------------------- #

_STMT_START_KEY = "_perf_stmt_start"


def _fingerprint(statement: str) -> str:
    """Safe, truncated, whitespace-collapsed SQL text. The DBAPI statement is
    already parameterized (placeholders like %(id)s, not literal values) — the
    literal values live separately in `parameters` and are never read here."""
    collapsed = " ".join(statement.split())
    return collapsed[:300]


def instrument_engine(engine: Engine) -> None:
    """Attaches query-timing, failure, and connection-hold-time instrumentation
    to an already-created engine. Call once, right after engine creation."""

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        conn.info.setdefault(_STMT_START_KEY, []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        stack = conn.info.get(_STMT_START_KEY)
        start = stack.pop() if stack else None
        duration_ms = (time.perf_counter() - start) * 1000 if start is not None else -1.0
        perf_counters.total_queries += 1
        if duration_ms >= SLOW_QUERY_MS:
            perf_counters.slow_queries += 1
            logger.warning(
                "sql_query duration_ms=%.1f SLOW fingerprint=%s",
                duration_ms,
                _fingerprint(statement),
            )

    @event.listens_for(engine, "handle_error")
    def _handle_error(exception_context):  # noqa: ANN001
        perf_counters.failed_queries += 1
        stmt = getattr(exception_context, "statement", None) or ""
        logger.error(
            "sql_query_failed error=%s fingerprint=%s",
            type(exception_context.original_exception).__name__,
            _fingerprint(stmt),
        )

    _CHECKOUT_AT_KEY = "perf_checkout_at"

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):  # noqa: ANN001
        connection_record.info[_CHECKOUT_AT_KEY] = time.perf_counter()

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, connection_record):  # noqa: ANN001
        checked_out_at = connection_record.info.pop(_CHECKOUT_AT_KEY, None)
        if checked_out_at is None:
            return
        held_ms = (time.perf_counter() - checked_out_at) * 1000
        if held_ms >= SLOW_CHECKOUT_MS:
            logger.warning("db_connection_held duration_ms=%.1f SLOW", held_ms)

    logger.info(
        "SQL instrumentation attached (slow_query_ms=%d, slow_checkout_ms=%d).",
        SLOW_QUERY_MS,
        SLOW_CHECKOUT_MS,
    )
