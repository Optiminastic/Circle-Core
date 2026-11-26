"""Phase 4 load test — LOCAL DEV ONLY (circle-be run against the Neon dev
database, see docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 4 for why: no
staging environment exists, and the Hetzner production VPS is shared with
~60 unrelated containers across multiple products, so synthetic load was
never run against it).

All simulated users share ONE pre-authenticated session cookie (see
scripts/seed_loadtest_data.py) rather than each logging in — `/api/auth/login`
has its own strict per-IP rate limit (6/min) that every simulated user
would collide on immediately from this single test machine's IP, which
would just measure the login rate limiter, not the app under load. Test
data is synthetic (`LoadTest ...` records created via the real API against
the dev DB) — never production records.

Run: locust -f scripts/locustfile_phase4.py --host=http://127.0.0.1:8020
"""

from __future__ import annotations

import json
import os
import random

from locust import HttpUser, between, tag, task

SEED_PATH = os.environ.get("LOADTEST_SEED_PATH", r"C:\Users\OPTIMI~1\AppData\Local\Temp\loadtest_seed.json")
with open(SEED_PATH) as f:
    SEED = json.load(f)

SESSION_COOKIE = SEED["session_cookie"]
CANDIDATE_IDS = SEED["candidates"]
JOB_IDS = SEED["jobs"]
EMPLOYEE_IDS = SEED["employees"]


class _AuthedUser(HttpUser):
    abstract = True

    def on_start(self) -> None:
        self.client.cookies.set("circle_session", SESSION_COOKIE)
        self.client.headers.update({"Origin": "http://localhost:3000"})


class DashboardUser(_AuthedUser):
    """Scenario A: concurrent dashboard users."""

    wait_time = between(1, 3)
    weight = 1

    @task
    def load_dashboard(self) -> None:
        self.client.get("/api/stats/overview", name="/api/stats/overview")
        self.client.get("/api/stats/upcoming-interviews?limit=6", name="/api/stats/upcoming-interviews")
        self.client.get(
            "/api/candidates?page=1&limit=10&sortBy=appliedAt&sortOrder=desc",
            name="/api/candidates?page=1&limit=10 (dashboard widget)",
        )


class CandidateListUser(_AuthedUser):
    """Scenario B: concurrent candidate list requests - both pagination modes."""

    wait_time = between(0.5, 2)
    weight = 1

    @task(2)
    def offset_page(self) -> None:
        self.client.get("/api/candidates?page=1&limit=50", name="/api/candidates?page=1&limit=50 (offset)")

    @task(1)
    def cursor_page(self) -> None:
        self.client.get("/api/candidates?cursor=&limit=50", name="/api/candidates?cursor= (keyset)")


class DocumentUploadUser(_AuthedUser):
    """Scenario C: simultaneous document uploads. Small synthetic payload,
    tagged loadtest- so it's identifiable/cleanable in the dev S3 bucket."""

    wait_time = between(1, 2)
    weight = 1

    @task
    def upload(self) -> None:
        if not CANDIDATE_IDS:
            return
        candidate_id = random.choice(CANDIDATE_IDS)
        payload = b"Load test synthetic document content - safe to delete.\n" * 20  # ~1.2KB
        self.client.post(
            "/api/documents",
            data={"entityType": "candidate", "entityId": candidate_id, "category": "loadtest"},
            files={"file": ("loadtest.txt", payload, "text/plain")},
            name="/api/documents (upload)",
        )


class BgvStatusUser(_AuthedUser):
    """Scenario D: concurrent BGV status checks. NOTE: local dev has no
    real OnGrid-linked candidate (no ongridIndividualId set), so this
    exercises the fast "foreign community / no record" short-circuit path
    in sync_ongrid_status, NOT the live-OnGrid-call + circuit-breaker path.
    Kept in the suite to at least measure route/auth/DB overhead; the
    OnGrid-call latency itself is production-only and wasn't load tested -
    see the Final Performance Report's caveats section."""

    wait_time = between(0.5, 1.5)
    weight = 1

    @task
    def check_status(self) -> None:
        if not CANDIDATE_IDS:
            return
        candidate_id = random.choice(CANDIDATE_IDS)
        self.client.get(f"/api/bgv/{candidate_id}/ongrid-status", name="/api/bgv/{id}/ongrid-status")


class DashboardDuringJobsUser(_AuthedUser):
    """Scenario E (approximated): concurrent dashboard load while the
    background job worker is active. OCR-as-a-background-job was
    explicitly scoped OUT of Phase 3 (see PERFORMANCE_IMPLEMENTATION_LOG.md)
    - there is no OCR job type to enqueue. This instead measures dashboard
    responsiveness while real background jobs (ongrid_status_sync, enqueued
    by the periodic scheduler every 5 minutes) are processing, which is the
    closest real equivalent this app currently has."""

    wait_time = between(1, 3)
    weight = 1

    @task
    def load_dashboard(self) -> None:
        self.client.get("/api/stats/overview", name="/api/stats/overview (during jobs)")
        self.client.get("/api/jobs?page=1&limit=50", name="/api/jobs?page=1&limit=50 (during jobs)")


class EmployeeSearchUser(_AuthedUser):
    """Scenario F: concurrent employee search, recruitment, onboarding ops."""

    wait_time = between(0.5, 2)
    weight = 1

    @task(2)
    def search_employees(self) -> None:
        self.client.get(
            "/api/employees?page=1&limit=50&search=LoadTest", name="/api/employees (search)"
        )

    @task(2)
    def search_candidates(self) -> None:
        self.client.get(
            "/api/candidates?page=1&limit=50&search=LoadTest", name="/api/candidates (search)"
        )

    @task(1)
    @tag("write")
    def patch_onboarding_field(self) -> None:
        if not EMPLOYEE_IDS:
            return
        employee_id = random.choice(EMPLOYEE_IDS)
        self.client.patch(
            f"/api/employees/{employee_id}",
            json={"notes": "loadtest touch"},
            name="/api/employees/{id} (patch)",
        )
