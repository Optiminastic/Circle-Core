"""Public, unauthenticated endpoints for the careers site.

This is the ONLY write surface the public is meant to use. Unlike the generic
CRUD router, it is deliberately narrow and hardened against abuse:

  * one atomic operation — submit a job application (resume + candidate) together;
  * a strict schema that REJECTS unknown fields (no mass-assignment), caps every
    string length, and validates email / phone / URL formats;
  * the server sets all trust-sensitive fields itself (id, status, source,
    applied date, role/department, fit rating) — the client cannot forge them;
  * the target job must exist and be Open;
  * the resume must be a real PDF (magic-byte checked) within the size limit;
  * text is sanitised (control chars + angle brackets stripped) to neutralise
    stored-XSS / HTML-injection into the HR dashboard and outgoing emails;
  * per-IP rate limited by the middleware in app.main.

Reads stay on the generic GET /api/jobs (served server-side by the careers app).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, field_validator

from app.api.dependencies import get_database, get_repository, get_storage
from app.core.config import Settings, get_settings
from app.core.errors import RateLimitedError, ValidationError
from app.core.logging import get_logger
from app.db.database import Database
from app.repositories.base import DocumentRepository
from app.repositories.document_repository import SqlAlchemyDocumentRepository
from app.services.email_sender import send_application_received, send_otp_email
from app.services.email_templates import resolve as resolve_template
from app.services import resume_keywords
from app.services.screening import build_answers, compute_fit
from app.storage.base import FileStorage

# These public, login-less endpoints are protected by abuse controls, not a
# secret: a per-email OTP cap + resend cooldown (below) and per-IP rate limits in
# app.main that apply to EVERY caller — website, server action, or a copied cURL.
router = APIRouter(prefix="/api/public", tags=["public"])

logger = get_logger("curcle.public")

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_LINKEDIN_RE = re.compile(r"^https?://([a-z0-9-]+\.)*linkedin\.com/.+", re.IGNORECASE)
_DRIVE_RE = re.compile(r"^https?://(drive|docs)\.google\.com/.+", re.IGNORECASE)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Max number of screening answers accepted (bounds payload + work).
_MAX_ANSWERS = 50
_MAX_ANSWER_LEN = 2000


def _clean(value: str) -> str:
    """Strip control chars and angle brackets, collapse, and trim.

    Angle brackets are removed so applicant text can never inject markup into the
    HR dashboard (defence in depth — React already escapes) or HTML emails.
    """
    return _CONTROL.sub("", value).replace("<", "").replace(">", "").strip()


# ── Email OTP verification ──────────────────────────────────────────────────
# Applicants must prove they own the email before they can apply. A 4-digit code
# is emailed and checked against a short-lived record in the "email_otps" table.
_OTP_TABLE = "email_otps"
_OTP_TTL = timedelta(minutes=10)  # how long a freshly issued code stays valid
_OTP_VERIFIED_TTL = timedelta(minutes=30)  # how long a verification lets you apply
_OTP_MAX_ATTEMPTS = 5
_OTP_ISSUE_WINDOW = timedelta(hours=1)  # rolling window for the per-email issue cap


def _norm_email(email: str) -> str:
    return email.strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log_sent_email(database: Database, row: dict[str, Any]) -> None:
    """Record a row in the same `sent_emails` resource the HR dashboard's Email
    Log reads from. Best-effort — a logging failure must never affect the send
    itself, and this runs on its own session since the request's is long closed."""
    session = database.session()
    try:
        SqlAlchemyDocumentRepository(session).upsert("sent_emails", row["id"], row)
    except Exception:  # noqa: BLE001 - logging only, never propagate
        logger.exception("Could not log sent-email row id=%s", row.get("id"))
    finally:
        session.close()


async def _send_application_received_after_delay(
    settings: Settings,
    database: Database,
    to: str,
    name: str,
    role: str,
    override: dict[str, str] | None,
    delay_minutes: int,
) -> None:
    """Wait, then send the acknowledgement. `asyncio.sleep` only pins a timer on
    the event loop (no thread held); the actual send is dispatched to a worker
    thread since `send_application_received` is a blocking SMTP/HTTP call.
    In-process only — a restart inside the delay window drops the pending send,
    same trade-off as every other best-effort BackgroundTask in this file."""
    if delay_minutes > 0:
        await asyncio.sleep(delay_minutes * 60)
    sent = await run_in_threadpool(send_application_received, settings, to, name, role, override)
    if not sent:
        return
    subject = override["subject"] if override else f"We received your application — {role.strip() or 'your role'}"
    await run_in_threadpool(
        _log_sent_email,
        database,
        {
            "id": f"EML-{uuid.uuid4().hex[:10]}",
            "recipientName": name,
            "recipientEmail": to,
            "templateTitle": "Application received",
            "subject": subject,
            "dateSent": datetime.now(timezone.utc).isoformat(),
            "status": "Sent",
            "relatedEntity": name,
        },
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class OtpRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v


class OtpVerifyIn(OtpRequestIn):
    code: str = Field(min_length=4, max_length=4)

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("invalid code")
        return v


def _email_is_verified(repo: DocumentRepository, email: str) -> bool:
    """True when the email has a recent, successful verification on record."""
    rec = repo.get(_OTP_TABLE, _norm_email(email))
    if not rec or not rec.get("verified"):
        return False
    verified_at = _parse_iso(rec.get("verifiedAt"))
    return verified_at is not None and _now() <= verified_at + _OTP_VERIFIED_TTL


def _already_applied(repo: DocumentRepository, job_id: str, email: str) -> bool:
    """True when this email already has an application for the given job."""
    target = email.strip().lower()
    existing = repo.find("candidates", {"jobId": job_id})
    return any((c.get("email") or "").strip().lower() == target for c in existing)


class AppliedCheckIn(OtpRequestIn):
    jobId: str = Field(min_length=1, max_length=64)


@router.post("/check-applied")
def check_applied(
    body: AppliedCheckIn,
    repo: DocumentRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Whether this email has already applied to this posting (one per role)."""
    return {"applied": _already_applied(repo, body.jobId, body.email)}


@router.post("/otp/request")
def otp_request(
    body: OtpRequestIn,
    background_tasks: BackgroundTasks,
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Generate a 4-digit code, store it, and email it to the applicant.

    Hardened against OTP spam / replay no matter how the request arrives (website,
    server action, or a copied cURL) — the limits live HERE on the server:
      * a per-email rolling cap on codes issued per hour;
      * a short resend cooldown so a held code can't be re-sent in a loop;
      * (the per-IP cap is enforced by the middleware in app.main).
    """
    email = _norm_email(body.email)
    now = _now()
    existing = repo.get(_OTP_TABLE, email) or {}

    # Keep only the issue timestamps still inside the rolling window.
    issued_times: list[str] = []
    for raw in existing.get("issuedTimes") or []:
        ts = _parse_iso(raw)
        if ts is not None and now - ts < _OTP_ISSUE_WINDOW:
            issued_times.append(raw)

    # Per-email hard cap: only N codes may be issued to one address per window.
    if len(issued_times) >= settings.otp_max_per_email_per_hour:
        logger.warning("OTP issue cap hit for %s (%d in window)", email, len(issued_times))
        raise RateLimitedError(
            "Too many verification codes requested for this email. Please try again later."
        )

    # Resend cooldown: swallow rapid re-requests (success, but no new code sent)
    # so a freshly issued code can't be re-mailed in a tight loop.
    parsed = [p for p in (_parse_iso(t) for t in issued_times) if p is not None]
    last_issued = max(parsed, default=None)
    cooldown = timedelta(seconds=settings.otp_resend_cooldown_seconds)
    if last_issued is not None and now - last_issued < cooldown:
        return {"ok": True}

    code = f"{secrets.randbelow(10000):04d}"
    issued_times.append(now.isoformat())
    repo.upsert(
        _OTP_TABLE,
        email,
        {
            "id": email,
            "email": email,
            "code": code,
            "issuedAt": now.isoformat(),
            "issuedTimes": issued_times,
            "expiresAt": (now + _OTP_TTL).isoformat(),
            "attempts": 0,
            "verified": False,
            "verifiedAt": None,
        },
    )
    logger.info(
        "OTP requested for %s (%d/%d in window)",
        email,
        len(issued_times),
        settings.otp_max_per_email_per_hour,
    )
    resp: dict[str, Any] = {"ok": True}
    if not settings.has_smtp:
        # No email transport configured (local dev) — return the code directly so
        # the flow stays testable. Never happens once a transport is set up.
        resp["devCode"] = code
        return resp
    # Send in the BACKGROUND so the HTTP response returns immediately — the SMTP
    # send can take a few seconds, and a long-pending request is fragile in the
    # browser (it can surface as "Failed to fetch"). Delivery failures are logged.
    background_tasks.add_task(send_otp_email, settings, email, code)
    return resp


@router.post("/otp/verify")
def otp_verify(
    body: OtpVerifyIn,
    repo: DocumentRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Check a code against the stored OTP and mark the email verified."""
    email = _norm_email(body.email)
    rec = repo.get(_OTP_TABLE, email)
    if rec is None:
        raise ValidationError("Request a verification code first.")
    expires = _parse_iso(rec.get("expiresAt"))
    if expires is None or _now() > expires:
        raise ValidationError("This code has expired. Please request a new one.")
    if int(rec.get("attempts", 0)) >= _OTP_MAX_ATTEMPTS:
        raise ValidationError("Too many attempts. Please request a new code.")
    if str(rec.get("code")) != body.code:
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        repo.upsert(_OTP_TABLE, email, rec)
        raise ValidationError("That code is incorrect. Please try again.")
    rec["verified"] = True
    rec["verifiedAt"] = _now().isoformat()
    repo.upsert(_OTP_TABLE, email, rec)
    logger.info("OTP verified for %s", email)
    return {"ok": True}


class ApplicationIn(BaseModel):
    # Reject any field not declared here — blocks mass-assignment (e.g. trying to
    # smuggle status="Hired", a custom id, or a forged fitRating).
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    jobId: str = Field(min_length=1, max_length=64)
    fullName: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(min_length=10, max_length=10)
    currentDesignation: str = Field(min_length=1, max_length=120)
    currentCtc: str = Field(min_length=1, max_length=40)
    expectedCtc: str = Field(min_length=1, max_length=40)
    totalExperienceYears: float = Field(ge=0, le=60)
    noticePeriodDays: int = Field(ge=0, le=3650)
    linkedInUrl: str = Field(min_length=1, max_length=300)
    coverNote: str = Field(min_length=1, max_length=5000)
    # Optional fields.
    location: str = Field(default="", max_length=120)
    # Male/Female/Other — captured for OnGrid BGV onboarding. Optional so older
    # clients don't break; validated to the known set when present.
    gender: str = Field(default="", max_length=10)
    currentCompany: str = Field(default="", max_length=120)
    resumeUrl: str = Field(default="", max_length=500)
    responses: dict[str, str] = Field(default_factory=dict)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("invalid phone")
        return v

    @field_validator("linkedInUrl")
    @classmethod
    def _linkedin(cls, v: str) -> str:
        if not _LINKEDIN_RE.match(v):
            raise ValueError("invalid LinkedIn URL")
        return v

    @field_validator("resumeUrl")
    @classmethod
    def _drive(cls, v: str) -> str:
        if v and not _DRIVE_RE.match(v):
            raise ValueError("invalid Google Drive URL")
        return v

    @field_validator("responses")
    @classmethod
    def _responses(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > _MAX_ANSWERS:
            raise ValueError("too many answers")
        return {
            str(k)[:64]: str(val)[:_MAX_ANSWER_LEN]
            for k, val in list(v.items())[:_MAX_ANSWERS]
        }


@router.post("/apply", status_code=201)
async def apply(
    background_tasks: BackgroundTasks,
    payload: str = Form(...),
    resume: UploadFile = File(...),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    # 1) Parse + validate the application (strict schema, length/format caps).
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        raise ValidationError("Invalid application data.")
    if not isinstance(raw, dict):
        raise ValidationError("Invalid application data.")
    try:
        app_in = ApplicationIn.model_validate(raw)
    except PydanticValidationError:
        # Don't echo internals back to the public — generic message only.
        raise ValidationError("Some details are missing or invalid. Please review the form.")

    # 2) The job must exist and be open to applications.
    job = repo.get("jobs", app_in.jobId)
    if job is None:
        raise ValidationError("This opening could not be found.")
    if job.get("status") != "Open":
        raise ValidationError("Applications for this opening are closed.")

    # 2b) The email must have been verified via OTP on this device/session.
    if not _email_is_verified(repo, app_in.email):
        raise ValidationError("Please verify your email before applying.")

    # 2c) One application per email, per posting — block re-applications.
    if _already_applied(repo, app_in.jobId, app_in.email):
        raise ValidationError("You have already applied to this role with this email.")

    # 3) Validate the resume bytes — must be a real, non-empty PDF within limit.
    data = await resume.read()
    if not data:
        raise ValidationError("Please attach your resume.")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationError(f"Your resume must be {settings.max_upload_mb} MB or smaller.")
    if not data.startswith(b"%PDF-"):
        raise ValidationError("Your resume must be a PDF file.")

    # 4) Server-owned identifiers + trust-sensitive fields (never from the client).
    candidate_id = f"CAN-{secrets.randbelow(900000) + 100000}"
    doc_id = uuid.uuid4().hex[:12]
    filename = (resume.filename or "resume.pdf")
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    key = f"documents/candidate/{candidate_id}/{doc_id}_{_SAFE_NAME.sub('_', filename)}"
    storage.put(key, data, "application/pdf")
    repo.upsert(
        "documents",
        doc_id,
        {
            "id": doc_id,
            "entityType": "candidate",
            "entityId": candidate_id,
            "category": "resume",
            "fileName": filename,
            "contentType": "application/pdf",
            "size": len(data),
            "storageKey": key,
            "uploadedAt": datetime.now(timezone.utc).isoformat(),
        },
    )

    # 5) Score screening answers server-side from the job's own questions.
    questions = job.get("screeningQuestions") or []
    answers = build_answers(questions, app_in.responses) if questions else []
    fit = compute_fit(answers) if answers else None

    # 5b) Resume keyword match against the job's keyword list, if any — best
    # effort (a scanned/image-only PDF yields no text, never blocks the
    # application). Snapshot at apply time, same as `fit` above: doesn't
    # retroactively change if HR edits the job's keywords later.
    resume_text = resume_keywords.extract_text(data)
    job_keywords = job.get("keywords") or []
    keyword_matches = resume_keywords.match_keywords(resume_text, job_keywords) if job_keywords else []

    # 6) Persist the candidate. Role/department come from the JOB, status/source/
    #    date are fixed by the server — the applicant can't influence the pipeline.
    candidate = {
        "id": candidate_id,
        "fullName": _clean(app_in.fullName),
        "email": app_in.email,
        "phone": f"+91 {app_in.phone}",
        "location": _clean(app_in.location),
        "gender": app_in.gender if app_in.gender in ("Male", "Female", "Other") else "",
        "currentCompany": _clean(app_in.currentCompany),
        "currentDesignation": _clean(app_in.currentDesignation),
        "totalExperienceYears": app_in.totalExperienceYears,
        "relevantExperienceYears": app_in.totalExperienceYears,
        "currentCtc": _clean(app_in.currentCtc),
        "expectedCtc": _clean(app_in.expectedCtc),
        "noticePeriodDays": app_in.noticePeriodDays,
        "resumeUrl": app_in.resumeUrl or filename,
        "linkedInUrl": app_in.linkedInUrl,
        "appliedRole": job.get("title", ""),
        "department": job.get("department", ""),
        "sourceOfApplication": "Job Posting",
        "referralDetails": f"Applied via public posting {app_in.jobId}",
        "hrRemarks": _clean(app_in.coverNote),
        "status": "New Application",
        "appliedDate": date.today().isoformat(),
        "appliedAt": datetime.now(timezone.utc).isoformat(),
        "jobId": app_in.jobId,
        "screeningAnswers": answers or None,
        "fitRating": fit,
        "resumeText": resume_text or None,
        # Always a list (possibly empty) once this code path has run at all —
        # the frontend tells "no score computed" (key absent, pre-feature
        # applicant) apart from "computed, scored zero" by checking for
        # undefined, not by emptiness. Never collapse [] to None here.
        "keywordMatches": keyword_matches,
    }
    repo.upsert("candidates", candidate_id, candidate)
    logger.info("Public application stored: candidate=%s job=%s", candidate_id, app_in.jobId)

    # 7) Consume the one-time email verification so it can't be reused, and email
    #    the applicant an automatic acknowledgement (best-effort, in background).
    repo.delete(_OTP_TABLE, _norm_email(app_in.email))
    # Resolve any HR-saved template now, while the request's session is still
    # open — the send itself runs after the response, when `repo` is closed.
    received_override = resolve_template(
        repo,
        "application_received",
        {
            "candidate_name": candidate["fullName"],
            "role": job.get("title", ""),
            "position": job.get("title", ""),
        },
    )
    background_tasks.add_task(
        _send_application_received_after_delay,
        settings,
        database,
        app_in.email,
        candidate["fullName"],
        job.get("title", ""),
        received_override,
        settings.application_received_delay_minutes,
    )

    # Return only an acknowledgement — never echo stored data back to the public.
    return {"ok": True}
