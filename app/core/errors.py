"""Domain error hierarchy + FastAPI exception handlers.

Keeping a small typed error hierarchy lets the API layer translate failures into
consistent, structured HTTP responses without leaking implementation details
(error tolerance + SRP).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .logging import get_logger

logger = get_logger("curcle.errors")


class AppError(Exception):
    """Base application error."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.__class__.__doc__ or "Unexpected error"
        super().__init__(self.detail)


class NotFoundError(AppError):
    """Requested resource was not found."""

    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    """The request payload was invalid."""

    status_code = 422
    code = "validation_error"


class AuthError(AppError):
    """The caller is not authorized to use this endpoint."""

    status_code = 403
    code = "forbidden"


class RateLimitedError(AppError):
    """Too many requests — slow down and retry later."""

    status_code = 429
    code = "rate_limited"


class RepositoryError(AppError):
    """The data store is unavailable or a query failed."""

    status_code = 503
    code = "repository_error"


class StorageError(AppError):
    """The object storage backend is unavailable or rejected the operation."""

    status_code = 502
    code = "storage_error"


#: What a 5xx tells whoever is on the page. The real detail names tables,
#: record ids and environment variables - "Failed to save 'doc_requests/<token>'",
#: "Set the AWS_* env vars and restart". That belongs in the log, not in front
#: of a candidate halfway through uploading their documents. A 4xx detail is
#: written for them and is kept.
SERVER_ERROR_DETAIL = "Something went wrong on our side. Please try again in a moment."


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        detail = exc.detail
        if exc.status_code >= 500:
            # Logged in full, answered in general.
            logger.exception("AppError: %s", detail)
            detail = SERVER_ERROR_DETAIL
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": detail})

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(status_code=500, content={"error": "internal_error", "detail": "Internal server error"})
