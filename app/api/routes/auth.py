"""Authentication + dashboard account management.

Login verifies a hashed password SERVER-SIDE and issues an httpOnly session
cookie — the plaintext password never leaves this endpoint and is never returned.
Legacy plaintext rows (from before hashing) are upgraded to a hash on first
successful login. All account-management endpoints require an admin session.

The `auth-users` resource is intentionally NOT reachable through the generic
`/api/{resource}` router (it's blocked there) — accounts are only touched here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from app.api.dependencies import get_resource_service, require_admin, require_user
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.domain.registry import get_resource
from app.services.password import hash_password, looks_hashed, verify_password
from app.services.resource_service import ResourceService
from app.services.sessions import COOKIE_NAME, issue_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

logger = get_logger("curcle.auth")

_AUTH_USERS = "auth-users"
EMAIL_MIN = 3


def _public_user(account: dict[str, Any]) -> dict[str, Any]:
    """Account view safe to return to the browser — never includes the password."""
    email = account.get("email") or account.get("id")
    return {
        "id": account.get("id") or email,
        "email": email,
        "role": account.get("role", "hr"),
        "name": account.get("name", ""),
    }


def _is_https(request: Request) -> bool:
    # Behind Caddy the app sees http; trust the proxy's X-Forwarded-Proto.
    return (
        request.headers.get("x-forwarded-proto", "").lower() == "https"
        or request.url.scheme == "https"
    )


def _set_session_cookie(request: Request, response: Response, settings: Settings, user: dict[str, Any]) -> None:
    token = issue_session(settings, email=user["email"], role=user["role"], name=user["name"])
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max(1, settings.session_ttl_hours) * 3600,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/",
    )


@router.post("/login")
def login(
    request: Request,
    response: Response,
    payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(get_settings),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")

    # Generic 401 (no user enumeration).
    invalid = HTTPException(status_code=401, detail="Invalid email or password.")
    try:
        account = service.get(get_resource(_AUTH_USERS), email)
    except NotFoundError:
        raise invalid

    stored = str(account.get("password", ""))
    if looks_hashed(stored):
        if not verify_password(password, stored):
            raise invalid
    else:
        # Legacy plaintext row — accept once if it matches, then upgrade to a hash.
        if stored != password:
            raise invalid
        service.patch(get_resource(_AUTH_USERS), email, {"password": hash_password(password)})

    user = _public_user(account)
    _set_session_cookie(request, response, settings, user)
    return user


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    # `user` is the validated session payload (email/role/name). Sliding session:
    # refresh the cookie on each app load so an active user is never logged out
    # (the expiry window advances by session_ttl_hours from now).
    _set_session_cookie(
        request,
        response,
        settings,
        {"email": user["email"], "role": user["role"], "name": user.get("name", "")},
    )
    return {"email": user["email"], "role": user["role"], "name": user.get("name", "")}


# --- Admin: account management (all require an admin session) ------------------

@router.get("/users")
def list_users(
    _admin: dict[str, Any] = Depends(require_admin),
    service: ResourceService = Depends(get_resource_service),
) -> list[dict[str, Any]]:
    return [_public_user(u) for u in service.list(get_resource(_AUTH_USERS))]


@router.post("/users", status_code=201)
def create_user(
    payload: dict[str, Any] = Body(...),
    _admin: dict[str, Any] = Depends(require_admin),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = payload.get("role", "hr")
    name = str(payload.get("name", "")).strip()
    if len(email) < EMAIL_MIN or "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if role not in ("admin", "hr"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'hr'.")
    doc = {"id": email, "email": email, "role": role, "name": name, "password": hash_password(password)}
    created = service.create(get_resource(_AUTH_USERS), doc)
    return _public_user(created)


@router.patch("/users/{email}/password")
def change_password(
    email: str,
    payload: dict[str, Any] = Body(...),
    _admin: dict[str, Any] = Depends(require_admin),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, bool]:
    new_password = str(payload.get("password", ""))
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    service.patch(get_resource(_AUTH_USERS), email.strip().lower(), {"password": hash_password(new_password)})
    return {"ok": True}


@router.patch("/users/{email}/email")
def change_email(
    email: str,
    payload: dict[str, Any] = Body(...),
    _admin: dict[str, Any] = Depends(require_admin),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    old = email.strip().lower()
    new = str(payload.get("newEmail", "")).strip().lower()
    if len(new) < EMAIL_MIN or "@" not in new:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if old == new:
        raise HTTPException(status_code=400, detail="That is already the account email.")
    if _exists(service, new):
        raise HTTPException(status_code=409, detail="That email is already in use.")
    account = service.get(get_resource(_AUTH_USERS), old)
    moved = {**account, "id": new, "email": new}
    service.create(get_resource(_AUTH_USERS), moved)
    service.delete(get_resource(_AUTH_USERS), old)
    return _public_user(moved)


@router.delete("/users/{email}", status_code=204, response_class=Response)
def delete_user(
    email: str,
    _admin: dict[str, Any] = Depends(require_admin),
    service: ResourceService = Depends(get_resource_service),
) -> Response:
    service.delete(get_resource(_AUTH_USERS), email.strip().lower())
    return Response(status_code=204)


def _exists(service: ResourceService, email: str) -> bool:
    try:
        service.get(get_resource(_AUTH_USERS), email)
        return True
    except NotFoundError:
        return False


def seed_admin_accounts(database: Any) -> None:
    """Create the first admin account on a fresh DB, from SEED_ADMIN_EMAIL /
    SEED_ADMIN_PASSWORD.

    Credentials are NEVER hardcoded here — a password committed to the repo is a
    password every reader of the repo (and its git history) knows. When the env
    vars are unset we seed nothing and log it: an app with no accounts is a
    recoverable state, an app with a publicly-known admin password is not.

    Idempotent — skips the account if it already exists, so existing installs and
    their current logins are untouched.
    """
    from app.repositories.document_repository import SqlAlchemyDocumentRepository

    settings = get_settings()
    email = (settings.seed_admin_email or "").strip().lower()
    password = settings.seed_admin_password or ""
    if not email or not password:
        logger.info("SEED_ADMIN_EMAIL/PASSWORD not set — skipping account seeding.")
        return

    session = database.session()
    try:
        service = ResourceService(SqlAlchemyDocumentRepository(session))
        if _exists(service, email):
            return
        service.create(
            get_resource(_AUTH_USERS),
            {
                "id": email,
                "email": email,
                "role": "admin",
                "name": settings.seed_admin_name or "Admin",
                "password": hash_password(password),
            },
        )
        logger.info("Seeded initial admin account %s.", email)
    finally:
        session.close()
