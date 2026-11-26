"""Python port of circle-fe/lib/pipeline.ts — "where is this candidate in
the pipeline?" This MUST stay behaviorally identical to that file, since
circle-fe's own detail page and Kanban derive a candidate's stage from the
same logic; the frontend file remains the single source of truth and this
is a faithful port of it, not a reinterpretation. See
docs/PERFORMANCE_IMPLEMENTATION_LOG.md's pagination-migration entry for why
this needed porting at all (so candidates/search can filter/sort by stage
server-side without downloading the full candidates + schedules +
interviews + iqTests + testInvites tables to the browser).

Deliberately NOT a SQL CASE expression: this logic has enough branches and
cross-field conditions that hand-translating it into SQL risked a subtle,
silent divergence from the canonical TypeScript behavior with no type
checker to catch it. Porting it to Python (same conditional structure,
same field names) is far easier to keep faithful and to test against the
TS source's own documented behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Document = dict[str, Any]

#: Mirrors circle-fe/lib/pipeline.ts's STAGE_STATUS_OPTIONS exactly (pipeline order).
STAGE_STATUS_OPTIONS: tuple[str, ...] = (
    "Screening",
    "HR Call",
    "Interview Scheduled",
    "IQ Test",
    "Assessment",
    "Physical Interview",
    "Hired",
    "On Hold",
    "Rejected",
)


@dataclass(frozen=True)
class PipelineContext:
    """One candidate's own schedules/interviews/iqTests/testInvites —
    already filtered to that candidate, not the whole table (see
    group_pipeline_context_by_candidate for the batch-efficient way to
    build these for every candidate in one pass)."""

    schedules: list[Document]
    interviews: list[Document]
    iq_tests: list[Document]
    invites: list[Document]


_EMPTY_CONTEXT = PipelineContext(schedules=[], interviews=[], iq_tests=[], invites=[])


def group_pipeline_context_by_candidate(
    schedules: list[Document],
    interviews: list[Document],
    iq_tests: list[Document],
    invites: list[Document],
) -> dict[str, PipelineContext]:
    """Groups all four child tables by candidateId ONCE, so computing every
    candidate's stage is O(n) total rather than O(candidates x child rows)
    from re-filtering each child list per candidate."""
    by_candidate: dict[str, dict[str, list[Document]]] = {}

    def bucket(rows: list[Document], key: str) -> None:
        for row in rows:
            cid = row.get("candidateId")
            if not cid:
                continue
            by_candidate.setdefault(cid, {"schedules": [], "interviews": [], "iqTests": [], "invites": []})
            by_candidate[cid][key].append(row)

    bucket(schedules, "schedules")
    bucket(interviews, "interviews")
    bucket(iq_tests, "iqTests")
    bucket(invites, "invites")

    return {
        cid: PipelineContext(
            schedules=buckets["schedules"],
            interviews=buckets["interviews"],
            iq_tests=buckets["iqTests"],
            invites=buckets["invites"],
        )
        for cid, buckets in by_candidate.items()
    }


def context_for(by_candidate: dict[str, PipelineContext], candidate_id: str) -> PipelineContext:
    return by_candidate.get(candidate_id, _EMPTY_CONTEXT)


def _pipeline_flags(candidate: Document, ctx: PipelineContext) -> dict[str, bool]:
    """Port of pipelineFlags() in lib/pipeline.ts — same variable names,
    same branches, same order, deliberately."""
    my_schedules = [s for s in ctx.schedules if s.get("status") != "Cancelled"]
    my_iq = ctx.iq_tests
    my_interviews = ctx.interviews
    my_invites = ctx.invites

    screening_started = bool(candidate.get("fitRating")) or bool(candidate.get("screeningReview"))
    screening_done = bool(candidate.get("fitRating"))

    hr_call_done = bool((candidate.get("hrCall") or {}).get("completed"))
    hr_call_reached = (
        hr_call_done
        or any(s.get("type") == "HR Call" for s in my_schedules)
        or candidate.get("status") == "Moved to HR Call"
    )

    iq_invite = next((i for i in my_invites if i.get("kind") == "iq"), None)
    iq_done = len(my_iq) > 0 or bool(iq_invite and iq_invite.get("status") in ("Completed", "Auto-Submitted"))
    iq_reached = iq_done or bool(iq_invite) or any(s.get("type") == "IQ Test" for s in my_schedules)

    # 'assignment'/'assessment' are aliases of the same MCQ stage (matches
    # candidate_stage_status below); 'take-home' is the separate file-based
    # assignment flow HR can send instead of an MCQ for this same stage.
    asg_invite = next(
        (i for i in my_invites if i.get("kind") in ("assessment", "assignment", "take-home")), None
    )
    # Take-home has no auto-score at submit time - 'Submitted' only means
    # HR's manual grading is pending, not done; only 'Graded' (or, for MCQ,
    # Completed/Auto-Submitted which score immediately) counts as finished.
    asg_done = bool(
        asg_invite
        and (
            asg_invite.get("status") == "Graded"
            if asg_invite.get("kind") == "take-home"
            else asg_invite.get("status") in ("Completed", "Auto-Submitted")
        )
    )
    asg_reached = bool(asg_invite) or any(s.get("type") == "Assessment" for s in my_schedules)

    interview_done = any(iv.get("status") == "Completed" for iv in my_interviews)
    interview_reached = len(my_interviews) > 0 or any(s.get("type") == "Interview" for s in my_schedules)

    offer_shortlisted = candidate.get("status") == "Offer Shortlisted"
    rejected = candidate.get("status") == "Rejected"
    selected = candidate.get("status") == "Selected"
    decided = rejected or selected
    on_hold = candidate.get("status") == "On Hold"

    return {
        "screeningStarted": screening_started,
        "screeningDone": screening_done,
        "hrCallReached": hr_call_reached,
        "hrCallDone": hr_call_done,
        "iqReached": iq_reached,
        "iqDone": iq_done,
        "asgReached": asg_reached,
        "asgDone": asg_done,
        "interviewReached": interview_reached,
        "interviewDone": interview_done,
        "offerShortlisted": offer_shortlisted,
        "rejected": rejected,
        "selected": selected,
        "decided": decided,
        "onHold": on_hold,
    }


def candidate_stage_status(candidate: Document, ctx: PipelineContext) -> str:
    """Port of candidateStageStatus() in lib/pipeline.ts — same precedence
    (highest reached stage wins, checked top-down), same stageDecisions
    overrides."""
    f = _pipeline_flags(candidate, ctx)
    if f["rejected"]:
        return "Rejected"
    if f["selected"]:
        return "Hired"
    if f["onHold"]:
        return "On Hold"

    stage_decisions = candidate.get("stageDecisions") or {}

    def accepted(label: str) -> bool:
        return stage_decisions.get(label) == "Accepted"

    my_interviews = ctx.interviews
    my_invites = ctx.invites
    # Match the detail page: the in-person round counts as "conducted" once
    # it's completed or feedback is recorded; the assessment stage accepts
    # either the 'assignment' or 'assessment' invite kind.
    interview_conducted = f["interviewDone"] or any(bool(iv.get("grading")) for iv in my_interviews)
    asg_reached = f["asgReached"] or any(i.get("kind") in ("assignment", "take-home") for i in my_invites)

    # Highest reached stage wins (checked top-down).
    if interview_conducted or accepted("Assessment"):
        return "Physical Interview"
    if asg_reached or accepted("IQ Test"):
        return "Assessment"
    if f["iqReached"] or accepted("Interview Schedule"):
        return "IQ Test"
    if f["interviewReached"] or accepted("HR Call"):
        return "Interview Scheduled"
    if f["hrCallReached"]:
        return "HR Call"
    return "Screening"
