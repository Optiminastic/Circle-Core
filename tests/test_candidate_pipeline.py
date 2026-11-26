"""candidate_stage_status — a faithful port of circle-fe/lib/pipeline.ts's
candidateStageStatus(). Every scenario here traces a specific branch of
that TS function (comments cite which), since a silent divergence from the
frontend's canonical stage logic would misclassify real candidates.
"""

from __future__ import annotations

from app.services.candidate_pipeline import (
    PipelineContext,
    candidate_stage_status,
    group_pipeline_context_by_candidate,
)

EMPTY = PipelineContext(schedules=[], interviews=[], iq_tests=[], invites=[])


def test_a_fresh_candidate_with_nothing_at_all_is_screening() -> None:
    assert candidate_stage_status({"status": "Applied"}, EMPTY) == "Screening"


def test_fit_rating_alone_does_not_advance_past_screening() -> None:
    # candidateStageStatus never reads fitRating/screeningReview directly -
    # only pipelineColumn (not ported, not needed here) does.
    assert candidate_stage_status({"status": "Applied", "fitRating": "Good Fit"}, EMPTY) == "Screening"


def test_an_hr_call_schedule_reaches_hr_call() -> None:
    ctx = PipelineContext(
        schedules=[{"type": "HR Call", "status": "Scheduled"}], interviews=[], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "HR Call"


def test_a_cancelled_hr_call_schedule_does_not_count() -> None:
    ctx = PipelineContext(
        schedules=[{"type": "HR Call", "status": "Cancelled"}], interviews=[], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Screening"


def test_status_moved_to_hr_call_reaches_hr_call_even_with_no_schedule() -> None:
    assert candidate_stage_status({"status": "Moved to HR Call"}, EMPTY) == "HR Call"


def test_an_interview_record_reaches_interview_scheduled() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[{"status": "Scheduled"}], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Interview Scheduled"


def test_an_iq_test_result_reaches_iq_test() -> None:
    ctx = PipelineContext(schedules=[], interviews=[], iq_tests=[{"totalQuestions": 20}], invites=[])
    assert candidate_stage_status({"status": "Applied"}, ctx) == "IQ Test"


def test_a_pending_iq_invite_also_reaches_iq_test() -> None:
    ctx = PipelineContext(schedules=[], interviews=[], iq_tests=[], invites=[{"kind": "iq", "status": "Pending"}])
    assert candidate_stage_status({"status": "Applied"}, ctx) == "IQ Test"


def test_an_assessment_invite_reaches_assessment() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[], iq_tests=[], invites=[{"kind": "assessment", "status": "Pending"}]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Assessment"


def test_an_assignment_invite_is_an_alias_for_assessment() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[], iq_tests=[], invites=[{"kind": "assignment", "status": "Pending"}]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Assessment"


def test_a_take_home_invite_also_reaches_assessment() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[], iq_tests=[], invites=[{"kind": "take-home", "status": "Submitted"}]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Assessment"


def test_a_completed_interview_reaches_physical_interview() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[{"status": "Completed"}], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Physical Interview"


def test_interview_feedback_without_completed_status_still_counts_as_conducted() -> None:
    # "conducted" = status Completed OR grading recorded - matches the detail page.
    ctx = PipelineContext(
        schedules=[], interviews=[{"status": "Scheduled", "grading": {"rating": 4}}], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Physical Interview"


def test_highest_reached_stage_wins_not_the_first_matched() -> None:
    # Both an HR Call schedule AND a completed interview exist - Physical
    # Interview (the furthest stage) must win, not HR Call.
    ctx = PipelineContext(
        schedules=[{"type": "HR Call", "status": "Scheduled"}],
        interviews=[{"status": "Completed"}],
        iq_tests=[],
        invites=[],
    )
    assert candidate_stage_status({"status": "Applied"}, ctx) == "Physical Interview"


def test_stage_decision_accepted_hr_call_reaches_interview_scheduled() -> None:
    candidate = {"status": "Applied", "stageDecisions": {"HR Call": "Accepted"}}
    assert candidate_stage_status(candidate, EMPTY) == "Interview Scheduled"


def test_stage_decision_accepted_interview_schedule_reaches_iq_test() -> None:
    candidate = {"status": "Applied", "stageDecisions": {"Interview Schedule": "Accepted"}}
    assert candidate_stage_status(candidate, EMPTY) == "IQ Test"


def test_stage_decision_accepted_iq_test_reaches_assessment() -> None:
    candidate = {"status": "Applied", "stageDecisions": {"IQ Test": "Accepted"}}
    assert candidate_stage_status(candidate, EMPTY) == "Assessment"


def test_stage_decision_accepted_assessment_reaches_physical_interview() -> None:
    candidate = {"status": "Applied", "stageDecisions": {"Assessment": "Accepted"}}
    assert candidate_stage_status(candidate, EMPTY) == "Physical Interview"


def test_a_rejected_decision_does_not_advance_the_stage() -> None:
    # A 'Rejected' stageDecision is not one of the four 'accepted' checks -
    # only the exact string 'Accepted' advances.
    candidate = {"status": "Applied", "stageDecisions": {"HR Call": "Rejected"}}
    assert candidate_stage_status(candidate, EMPTY) == "Screening"


def test_candidate_status_rejected_wins_over_every_pipeline_signal() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[{"status": "Completed"}], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "Rejected"}, ctx) == "Rejected"


def test_candidate_status_selected_is_hired_regardless_of_pipeline_signals() -> None:
    assert candidate_stage_status({"status": "Selected"}, EMPTY) == "Hired"


def test_candidate_status_on_hold_wins_over_pipeline_signals_but_not_rejected() -> None:
    ctx = PipelineContext(
        schedules=[], interviews=[{"status": "Completed"}], iq_tests=[], invites=[]
    )
    assert candidate_stage_status({"status": "On Hold"}, ctx) == "On Hold"


def test_group_pipeline_context_by_candidate_isolates_each_candidates_own_rows() -> None:
    schedules = [
        {"candidateId": "A", "type": "HR Call", "status": "Scheduled"},
        {"candidateId": "B", "type": "IQ Test", "status": "Scheduled"},
    ]
    interviews = [{"candidateId": "A", "status": "Completed"}]
    grouped = group_pipeline_context_by_candidate(schedules, interviews, [], [])

    assert candidate_stage_status({"id": "A", "status": "Applied"}, grouped["A"]) == "Physical Interview"
    assert candidate_stage_status({"id": "B", "status": "Applied"}, grouped["B"]) == "IQ Test"
    # A candidate with no rows at all gets the empty context, not someone
    # else's schedules/interviews.
    assert candidate_stage_status({"id": "C", "status": "Applied"}, grouped.get("C", EMPTY)) == "Screening"
