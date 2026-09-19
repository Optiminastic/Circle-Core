"""Resource registry — the single declaration of the HR resources the API serves.

Adding a new resource is a one-line change here (Open/Closed): the generic
repository, service and router pick it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import NotFoundError


@dataclass(frozen=True)
class ResourceDef:
    slug: str       # URL segment, e.g. "iq-tests"
    table: str      # physical table name, e.g. "iq_tests"
    id_field: str   # field within the document used as the primary key


_DEFS: tuple[ResourceDef, ...] = (
    ResourceDef("auth-users", "auth_users", "id"),
    ResourceDef("schedules", "schedules", "id"),
    ResourceDef("jobs", "jobs", "id"),
    ResourceDef("candidates", "candidates", "id"),
    ResourceDef("test-invites", "test_invites", "id"),
    ResourceDef("doc-requests", "doc_requests", "id"),
    ResourceDef("interviews", "interviews", "id"),
    ResourceDef("iq-tests", "iq_tests", "id"),
    ResourceDef("assignments", "assignments", "id"),
    ResourceDef("bgvs", "bgvs", "candidateId"),
    ResourceDef("onboarding", "onboarding", "candidateId"),
    ResourceDef("employees", "employees", "id"),
    ResourceDef("assets", "assets", "id"),
    ResourceDef("email-templates", "email_templates", "id"),
    # HR-edited overrides for the built-in transactional emails, keyed by template
    # id (e.g. "iq_test_invite"). Only templates HR has actually edited get a row;
    # everything else falls back to the built-in copy. See services/email_templates.
    ResourceDef("email-template-overrides", "email_template_overrides", "id"),
    ResourceDef("sent-emails", "sent_emails", "id"),
    ResourceDef("offboarding", "offboarding", "employeeId"),
    ResourceDef("exit-handovers", "exit_handovers", "employeeId"),
    # Public, token-gated handoff of a hired candidate's profile + documents to an
    # external onboarding system (created by HR, read by the other app).
    ResourceDef("candidate-handoffs", "candidate_handoffs", "candidateId"),
    # Google Calendar: single shared-account OAuth row + per-event id mapping.
    ResourceDef("google-oauth", "google_oauth", "id"),
    ResourceDef("calendar-links", "calendar_links", "id"),
    # Question Library banks — created by HR, shared across devices (replaces the
    # old per-browser localStorage). One record per bank; iq-bank is a singleton.
    ResourceDef("assessment-banks", "assessment_banks", "id"),
    ResourceDef("interview-banks", "interview_banks", "id"),
    ResourceDef("screening-banks", "screening_banks", "id"),
    ResourceDef("iq-bank", "iq_bank", "id"),
    # Interviewer feedback sheet payloads — stored under a short unguessable token
    # so the link emailed to an interviewer stays short (instead of base64-encoding
    # the whole payload into the URL). Read by the public /interview-sheet page.
    ResourceDef("interview-sheets", "interview_sheets", "id"),
    # Public, token-gated joining-date confirmation + welcome preferences
    # (meal, plant) sent to a hired candidate. Created by HR, read/patched by
    # the candidate via /api/joining-confirmations/*.
    ResourceDef("joining-confirmations", "joining_confirmations", "id"),
)

RESOURCES: dict[str, ResourceDef] = {d.slug: d for d in _DEFS}


def get_resource(slug: str) -> ResourceDef:
    resource = RESOURCES.get(slug)
    if resource is None:
        raise NotFoundError(f"Unknown resource '{slug}'. Valid: {', '.join(RESOURCES)}")
    return resource


def all_tables() -> list[str]:
    return [d.table for d in _DEFS]
