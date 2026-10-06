from app.services.screening import build_answers, compute_fit
from app.services.screening_call_results import score_call

MUST = "Must Have"
GOOD = "Good to Have"

QUESTIONS = [
    {"id": "m1", "text": "Use Instagram?", "type": "yesno", "importance": MUST, "expectedAnswer": True},
    {"id": "m2", "text": "Comfortable on camera?", "type": "yesno", "importance": MUST, "expectedAnswer": True},
    {"id": "g1", "text": "Internship?", "type": "yesno", "importance": GOOD, "expectedAnswer": True},
    {"id": "g2", "text": "Experience?", "type": "choice", "importance": GOOD,
     "options": ["0-1 years", "1-2 years"], "expectedOption": "1-2 years"},
    {"id": "g3", "text": "Used lights?", "importance": GOOD, "expectedAnswer": True},
]


def entry(answer, evidence="quote", strength="strong"):
    return {"answer": answer, "evidence": evidence, "followUpStrength": strength}


def data(*answers):
    return {f"q{i + 1}": entry(a) for i, a in enumerate(answers)}


def test_all_yes_is_fit() -> None:
    result = score_call(QUESTIONS, data("Yes", "Yes", "Yes", "1-2 years", "Yes"), form_answers=None)
    assert result.fit_rating == "Fit"
    assert result.needs_review is False
    assert result.declined is False
    assert [a["answer"] for a in result.answers] == ["Yes", "Yes", "Yes", "1-2 years", "Yes"]


def test_one_must_have_no_is_unfit() -> None:
    result = score_call(QUESTIONS, data("Yes", "No", "Yes", "1-2 years", "Yes"), form_answers=None)
    assert result.fit_rating == "Unfit"


def test_one_of_three_good_to_have_is_borderline() -> None:
    result = score_call(QUESTIONS, data("Yes", "Yes", "No", "0-1 years", "Yes"), form_answers=None)
    assert result.fit_rating == "Borderline"


def test_same_answers_rate_the_same_as_the_web_form() -> None:
    responses = {"m1": "Yes", "m2": "Yes", "g1": "No", "g2": "1-2 years", "g3": "No"}
    form_fit = compute_fit(build_answers(QUESTIONS, responses))
    call = score_call(QUESTIONS, data(*responses.values()), form_answers=None)
    assert call.fit_rating == form_fit


def test_unclear_and_missing_answers_need_review_and_never_pass() -> None:
    questions = [{"id": "x", "text": "Avoid travel?", "type": "yesno", "importance": MUST, "expectedAnswer": False}]
    result = score_call(questions, {"q1": entry("Unclear")}, form_answers=None)
    assert result.answers[0]["passed"] is False  # "Unclear" must not pass a No-expected question
    assert result.needs_review is True
    missing = score_call(QUESTIONS, {}, form_answers=None)
    assert all(a["answer"] == "Unclear" for a in missing.answers)
    assert missing.needs_review is True


def test_off_list_answers_become_unclear() -> None:
    result = score_call(QUESTIONS, data("haan", "Yes", "Yes", "two years", "Yes"), form_answers=None)
    assert result.answers[0]["answer"] == "Unclear"
    assert result.answers[3]["answer"] == "Unclear"


def test_evidence_and_follow_up_strength_are_kept_and_sanitised() -> None:
    structured = data("Yes", "Yes", "Yes", "1-2 years", "Yes")
    structured["q1"] = entry("Yes", evidence="x" * 5000, strength="bogus")
    structured["q2"] = entry("Yes", strength="weak")
    result = score_call(QUESTIONS, structured, form_answers=None)
    assert len(result.answers[0]["evidence"]) <= 500
    assert result.answers[0]["followUpStrength"] == "n/a"
    assert result.answers[1]["followUpStrength"] == "weak"


def test_contradiction_with_form_is_flagged() -> None:
    form = [{"questionId": "m1", "answer": "Yes"}, {"questionId": "m2", "answer": "Yes"}]
    result = score_call(QUESTIONS, data("No", "Yes", "Yes", "1-2 years", "Unclear"), form_answers=form)
    assert result.answers[0]["formAnswer"] == "Yes"
    assert result.answers[0]["contradictsForm"] is True
    assert result.answers[1]["contradictsForm"] is False
    assert result.answers[4]["formAnswer"] is None
    assert result.answers[4]["contradictsForm"] is False


def test_declined_call_has_no_rating() -> None:
    result = score_call(QUESTIONS, {"declined": True}, form_answers=None)
    assert result.declined is True
    assert result.fit_rating is None


def test_text_answers_never_fail() -> None:
    questions = [{"id": "t", "text": "Why us?", "type": "text", "importance": MUST}]
    result = score_call(questions, {"q1": entry("I love content")}, form_answers=None)
    assert result.answers[0]["passed"] is True
    assert result.fit_rating == "Fit"


def test_garbage_structured_data_is_tolerated() -> None:
    result = score_call(QUESTIONS, {"q1": "Yes", "q2": None, "q3": {"answer": 5}}, form_answers=None)
    assert [a["answer"] for a in result.answers][:3] == ["Unclear", "Unclear", "Unclear"]


def test_yes_no_question_ignores_leftover_options() -> None:
    # A question switched from choice to yes/no in the job editor can keep its old options.
    questions = [{"id": "y", "text": "Q?", "type": "yesno", "importance": MUST,
                  "expectedAnswer": True, "options": ["A", "B"]}]
    assert score_call(questions, {"q1": entry("Yes")}, form_answers=None).answers[0]["answer"] == "Yes"
    assert score_call(questions, {"q1": entry("A")}, form_answers=None).answers[0]["answer"] == "Unclear"
