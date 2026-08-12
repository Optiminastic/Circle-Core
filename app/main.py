"""Application entrypoint / composition root.

Builds the FastAPI app, owns the database lifecycle, registers middleware,
exception handlers and routers. This is the only module that knows how all the
layers fit together (composition root pattern).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    auth,
    bgv_ongrid,
    calendar,
    candidate_delete,
    candidate_handoff,
    candidate_promote,
    doc_requests,
    documents,
    exit_handover,
    interview_public,
    meta,
    notifications,
    public,
    resources,
    test_public,
)
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import SlidingWindowRateLimiter, client_ip, is_exempt_origin
from app.db.database import Database
from app.domain.registry import all_tables
from app.storage.s3_storage import S3FileStorage

logger = get_logger("curcle.main")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings)
        database.connect()
        if settings.auto_create_tables:
            database.ensure_tables([*all_tables(), "documents", "email_otps"])
            # Seed the default dashboard accounts (hashed) on a fresh DB. Existing
            # accounts are left as-is (legacy plaintext rows are upgraded to a hash
            # on their next successful login).
            auth.seed_admin_accounts(database)
        app.state.database = database

        if settings.has_storage:
            app.state.storage = S3FileStorage(settings)
            logger.info("Object storage configured (bucket=%s).", settings.aws_bucket_name)
        else:
            app.state.storage = None
            logger.warning("S3 storage not configured — document endpoints will return 502.")

        logger.info("%s v%s started.", settings.app_name, settings.app_version)
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Backend for the Optiminastic × Circle HR Operating System — PostgreSQL source of truth.",
        lifespan=lifespan,
    )

    # --- Anti-spam: per-IP rate limit on the PUBLIC, unauthenticated writes ----
    # The careers form lets anyone create a candidate + upload a resume; throttle
    # those by client IP so the openings can't be spammed. Registered BEFORE CORS
    # below so the CORS middleware (added last = outermost) still attaches headers
    # to the 429, letting the browser read the message. HR + local origins exempt.
    public_write_limiter = SlidingWindowRateLimiter(
        [
            (settings.public_rate_limit_per_minute, 60.0),
            (settings.public_rate_limit_per_hour, 3600.0),
        ]
    )
    # Separate, looser limiter for public token-gated FILE uploads so a legitimate
    # multi-file handover (one POST per file) isn't throttled.
    public_upload_limiter = SlidingWindowRateLimiter(
        [
            (settings.upload_rate_limit_per_minute, 60.0),
            (settings.upload_rate_limit_per_hour, 3600.0),
        ]
    )
    # Public token-gated READS that reveal PII (doc-request details incl. bank
    # account, and document bytes/URLs) + the candidate's own doc-request PATCH.
    # Bounds scraping if a link leaks, but generous enough that HR reviewing a
    # candidate's documents (the panel polls every 20s + opens each file) is never
    # throttled. Its own counter, so it doesn't compete with the upload budget.
    public_read_limiter = SlidingWindowRateLimiter(
        [
            (settings.portal_read_rate_limit_per_minute, 60.0),
            (settings.portal_read_rate_limit_per_hour, 3600.0),
        ]
    )
    public_write_paths = {"/api/public/apply", "/api/candidates", "/api/documents", "/api/auth/login"}

    def _is_public_upload(path: str) -> bool:
        """Public, token-gated file-upload endpoints (onboarding + exit handover)."""
        return (path.startswith("/api/exit-handovers/portal/") and path.endswith("/upload")) or (
            path.startswith("/api/doc-requests/") and path.endswith("/upload")
        )

    def _is_public_credential(path: str) -> bool:
        """Public, token-gated exit-handover credential submission."""
        return path.startswith("/api/exit-handovers/portal/") and path.endswith("/credentials")

    def _is_candidate_feed(path: str) -> bool:
        """Public, token-gated candidate feed (GET) — exposes PII, so it's per-IP
        rate limited so a leaked URL can't be scraped at speed."""
        return path.startswith("/api/candidate-feed/")

    def _is_public_portal_read(method: str, path: str) -> bool:
        """Public, token-gated endpoints that REVEAL PII or accept the candidate's
        own doc-request writes. Same reasoning as the candidate feed: a leaked
        link must not be scrapable at speed. Covers:
          - GET/PATCH /api/doc-requests/{token}  (details incl. bank acct; consent/
            bank/references saves).  NOT the /upload sub-path (POST, handled above).
          - GET /api/documents/{id}/preview | /url  (document bytes / presigned URL).
        """
        if method == "GET" or method == "PATCH":
            if path.startswith("/api/doc-requests/") and not path.endswith("/upload"):
                return True
        if method == "GET" and path.startswith("/api/documents/"):
            return path.endswith("/preview") or path.endswith("/url")
        return False
    # NOTE: the OTP / email-check endpoints are intentionally NOT per-IP rate
    # limited. Many candidates can legitimately request a code at the same time —
    # from a shared office/college network, or all via the careers server's single
    # egress IP — and a per-IP cap would wrongly throttle that burst. OTP abuse is
    # bounded PER-EMAIL instead (a cap + resend cooldown in public.py), which never
    # blocks a different candidate's email.
    # Only the HR-app write paths honour the trusted-origin exemption (so staff
    # aren't throttled); the public apply path is always throttled per IP.
    hr_write_paths = {"/api/candidates", "/api/documents"}

    def _too_many(ip: str, path: str) -> JSONResponse:
        logger.warning("Rate limit hit: ip=%s path=%s", ip, path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please wait a minute and try again."},
            headers={"Retry-After": "60"},
        )

    @app.middleware("http")
    async def rate_limit_public_writes(request: Request, call_next):
        path = request.url.path
        if settings.rate_limit_enabled and request.method == "POST":
            ip = client_ip(request)
            # Public token-gated uploads — never origin-exempt; looser per-IP cap.
            if _is_public_upload(path):
                if not public_upload_limiter.allow(ip):
                    return _too_many(ip, path)
            # Apply, credential submission, and HR writes — strict per-IP cap.
            elif path in public_write_paths or _is_public_credential(path):
                exempt = path in hr_write_paths and is_exempt_origin(
                    request.headers.get("origin", ""), settings
                )
                if not exempt and not public_write_limiter.allow(ip):
                    return _too_many(ip, path)
        # The public candidate feed (GET) exposes PII — throttle per IP too.
        elif settings.rate_limit_enabled and request.method == "GET" and _is_candidate_feed(path):
            ip = client_ip(request)
            if not public_write_limiter.allow(ip):
                return _too_many(ip, path)
        # Public token-gated reads/patches that reveal PII (doc-request details,
        # document bytes) — looser cap, so a leaked link can't be scraped fast.
        elif settings.rate_limit_enabled and _is_public_portal_read(request.method, path):
            ip = client_ip(request)
            if not public_read_limiter.allow(ip):
                return _too_many(ip, path)
        return await call_next(request)

    # Compress large list payloads — JSONB resource lists shrink ~5-10x.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Also allow localhost and any private-LAN origin (any port), so the app
        # works when opened from a phone/other device on the same network. The
        # matched origin is echoed back (never "*"), so allow_credentials is safe.
        allow_origin_regex=(
            r"https?://(localhost|127\.0\.0\.1|"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?"
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(meta.router)
    # Documents/notifications routers must precede the generic resources router
    # so their literal paths win over "/api/{resource}".
    app.include_router(documents.router)
    app.include_router(doc_requests.router)
    app.include_router(bgv_ongrid.router)
    app.include_router(notifications.router)
    app.include_router(calendar.router)
    # Auth (login/logout/me + admin account mgmt) — its own /api/auth/* prefix.
    app.include_router(auth.router)
    # Hardened public router must precede the generic resources router so its
    # literal /api/public/* paths win over "/api/{resource}".
    app.include_router(public.router)
    # Public interviewer-sheet endpoints (/api/public/interview-sheet/*).
    app.include_router(interview_public.router)
    # Public candidate test endpoints (/api/public/test/*) — write-once results.
    app.include_router(test_public.router)
    # Token-gated exit-handover routes must precede the generic resources router.
    app.include_router(exit_handover.router)
    # Candidate-handoff "mark arrived" must precede the generic resources router so
    # "/{candidate_id}/send" wins over "/api/candidate-handoffs/{id}".
    app.include_router(candidate_handoff.router)
    # Public token-gated candidate feed (separate prefix, no resource conflict).
    app.include_router(candidate_handoff.feed_router)
    # Cascade delete for candidates must precede the generic resources router so
    # DELETE /api/candidates/{id} cleans up related records (other methods fall
    # through to the generic router).
    app.include_router(candidate_delete.router)
    app.include_router(candidate_promote.router)
    app.include_router(resources.router)
    return app


app = create_app()


if __name__ == "__main__":
    # Production entrypoint (e.g. Render): bind 0.0.0.0 on the platform-provided
    # $PORT so the host can detect the open port. Locally falls back to 8000.
    import os

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - must bind all interfaces for the platform health check
        port=int(os.environ.get("PORT", "8000")),
    )
