"""Application configuration (12-factor, env-driven).

Single source of truth for settings; everything else depends on the `Settings`
abstraction rather than reading env vars directly (DIP).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Optiminastic × Circle HRMS API"
    app_version: str = "2.0.0"
    log_level: str = "INFO"

    # PostgreSQL connection string. Required to serve data.
    database_url: str = ""
    auto_create_tables: bool = True

    # DB pool tuning (scalability). pool_size is sized to the post-login
    # prefetch burst so overflow connections (which are torn down on check-in
    # and pay a fresh TLS handshake next time) are rarely needed.
    db_pool_size: int = 12
    db_max_overflow: int = 8
    db_pool_timeout: int = 30
    # Recycle pooled connections before the server/pooler idle-closes them
    # (Neon's pooler kills idle server connections after ~5 min).
    db_pool_recycle: int = 240

    # NoDecode: skip pydantic-settings' JSON decoding so the validator can accept
    # a plain comma-separated string from .env.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000", "http://localhost:3001"]

    # AWS S3 object storage. S3-compatible providers also work via AWS_S3_ENDPOINT.
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = ""
    aws_bucket_name: str = ""
    # Optional custom endpoint for S3-compatible providers; empty = real AWS S3.
    aws_s3_endpoint: str = ""
    # Optional key prefix (a "folder") all objects are stored under, e.g. "Circle".
    aws_s3_prefix: str = ""
    max_upload_mb: int = 15

    # Optional dedicated key for encrypting sensitive at-rest fields (exit-handover
    # credentials). If unset, a key is derived from existing app secrets.
    credentials_key: str = ""

    # SMTP for candidate notification emails. Gmail: user = the full address
    # (which is also the From sender), password = a Gmail app password.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""  # login username (Gmail address, or literally "apikey")
    smtp_password: str = ""  # Gmail app password, or the provider API key
    smtp_from_email: str = ""  # verified sender; falls back to smtp_user (Gmail)
    smtp_from_name: str = "Optiminastic Careers"
    # Where candidate replies should land (Reply-To header). Falls back to the
    # From address when unset.
    smtp_reply_to: str = "hr@optiminastic.com"
    # Always CC HR on recruitment correspondence (interview invites, the Physical
    # Interview pack, test invites/results, decisions). Blank disables it.
    hr_cc_email: str = "hr@optiminastic.com"
    # Resend HTTP API key (used INSTEAD of SMTP when set — Render free tier blocks
    # outbound SMTP). Auto-derived from the Resend SMTP password if not set.
    resend_api_key: str = ""
    # Delay before the "application received" acknowledgement goes out, so it
    # doesn't arrive the instant the candidate submits. In-process timer (see
    # public.py) — not durable across a restart within the delay window.
    application_received_delay_minutes: int = 30

    # OnGrid background-verification API (Basic auth). Onboards a hired candidate
    # into a community; verifications themselves are triggered by HR in OnGrid's
    # portal. base_url points at the /app basePath. See app/services/ongrid.py.
    ongrid_base_url: str = "https://api-staging.ongrid.in/app"
    ongrid_username: str = ""
    ongrid_password: str = ""
    ongrid_community_id: str = ""

    # Anti-spam rate limiting for the PUBLIC, unauthenticated writes (job
    # application: candidate create + resume upload). Limits are per client IP.
    # The HR app origin and local/LAN origins are exempt (see rate_limit.py), so
    # only the public careers traffic (and direct API abuse) is throttled.
    rate_limit_enabled: bool = True
    public_rate_limit_per_minute: int = 6  # ~3 applications/min/IP (2 calls each)
    public_rate_limit_per_hour: int = 30  # ~15 applications/hour/IP
    # Looser caps for public token-gated FILE uploads (onboarding docs + exit
    # handover) — a single handover can be many files submitted in quick
    # succession (one POST per file), so it needs more headroom than apply.
    upload_rate_limit_per_minute: int = 40
    upload_rate_limit_per_hour: int = 200
    # Per-IP caps for public token-gated READS that reveal PII (doc-request
    # details incl. bank account, and document bytes/URLs). Bounds scraping of a
    # leaked link, but deliberately generous: HR reviews these same public
    # endpoints interactively (opening each candidate's documents), so the cap
    # must leave ample headroom for a busy reviewer on one IP.
    portal_read_rate_limit_per_minute: int = 60
    portal_read_rate_limit_per_hour: int = 600
    # Per-IP caps for the OTP / email-check endpoints (looser than apply, since a
    # single applicant makes a few calls: check + request + verify [+ resend]).
    otp_rate_limit_per_minute: int = 10
    otp_rate_limit_per_hour: int = 40
    # Per-EMAIL caps on OTP generation (independent of IP) — stops email-bombing a
    # single address even from many IPs. See app/api/routes/public.py.
    otp_max_per_email_per_hour: int = 5
    otp_resend_cooldown_seconds: int = 30
    # The HR dashboard origin — requests from here are never rate-limited.
    hr_app_origin: str = "https://circle.optiminastic.com"

    # Office location for offline rounds (IQ Test / Assessment / Interview).
    office_address: str = "Optiminastic Office (set OFFICE_ADDRESS in .env)"
    office_maps_url: str = "https://maps.google.com/?q=Optiminastic"

    # Google Calendar (single shared HR account, one-way push). The OAuth client
    # id/secret come from a Google Cloud "Web application" credential; the shared
    # account's refresh token is obtained via the in-app connect flow and stored
    # in the DB (never in env). See app/services/google_calendar.py.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/calendar/oauth/callback"
    google_calendar_id: str = "primary"  # which calendar events are written to
    # Where the OAuth callback redirects the browser back to (the Settings page).
    frontend_url: str = "http://localhost:3001"

    # Candidate handoff feed — Curcle hosts ONE public, token-gated URL that an
    # external onboarding system fetches to get every candidate HR has marked
    # "arrived in office" (curated fields + verified document links). The token is
    # the secret embedded in the feed URL (/api/candidate-feed/<token>). Empty =
    # feed disabled (returns 404).
    candidate_feed_token: str = ""

    # --- Auth / dashboard sessions ---
    # Secret used to sign the httpOnly session cookie. Set SESSION_SECRET in prod
    # for a stable dedicated key; otherwise it falls back to an existing secret
    # (rotating that secret just invalidates existing sessions, which is fine).
    session_secret: str = ""
    # How long a login stays valid. Sliding: every app load (/api/auth/me) refreshes
    # the cookie, so an active user stays signed in indefinitely and only has to log
    # in again after this many hours of NO activity. Default 30 days.
    session_ttl_hours: int = 720

    # --- First-run admin seeding ---
    # Used ONLY to create the very first admin on an empty DB. Unset = seed nothing.
    # Never hardcode credentials in source; see auth.seed_admin_accounts.
    seed_admin_email: str = ""
    seed_admin_password: str = ""
    seed_admin_name: str = "Admin"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def sqlalchemy_url(self) -> str:
        """Normalize the URL to the psycopg (v3) driver SQLAlchemy expects."""
        url = self.database_url.strip()
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://"):]
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://"):]
        return url

    @property
    def has_database(self) -> bool:
        return bool(self.database_url.strip())

    @property
    def has_storage(self) -> bool:
        return bool(
            self.aws_access_key_id
            and self.aws_secret_access_key
            and self.aws_bucket_name
            and self.aws_region
        )

    @property
    def resend_key(self) -> str:
        """The Resend API key for the HTTPS transport.

        Uses RESEND_API_KEY if set, otherwise reuses the SMTP password when the
        SMTP config points at Resend (host smtp.resend.com, user 'resend'). Lets
        an existing Resend-over-SMTP setup switch to the API automatically on
        hosts that block SMTP (e.g. Render) — no new env var needed.
        """
        if self.resend_api_key:
            return self.resend_api_key
        if "resend" in self.smtp_host.lower() and self.smtp_user.lower() == "resend":
            return self.smtp_password
        return ""

    @property
    def has_smtp(self) -> bool:
        # True when an email transport is configured (Resend HTTP or SMTP).
        return bool(self.resend_key or (self.smtp_user and self.smtp_password))

    @property
    def from_address(self) -> str:
        """The envelope From email. Defaults to smtp_user (Gmail's login address
        is also the sender)."""
        return self.smtp_from_email or self.smtp_user

    @property
    def has_google(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def has_candidate_feed(self) -> bool:
        return bool(self.candidate_feed_token.strip())

    @property
    def has_ongrid(self) -> bool:
        return bool(
            self.ongrid_username and self.ongrid_password and self.ongrid_community_id
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
