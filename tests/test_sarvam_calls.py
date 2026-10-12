"""Placing a screening call through Sarvam Instant Outbound.

The variable names and the shape of `questions` are not ours to choose - they
are read from the agent's committed config. Sarvam accepts a variable the
agent never declared without complaint and it arrives at the call as nothing,
so a wrong name here is a call that rings a real candidate and asks them
nothing. These tests pin the contract.

No network: the transport is replaced.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.sarvam_calls import (
    MAX_QUESTIONS,
    SarvamCallProvider,
    SarvamCallSettings,
    format_questions,
)
from app.services.voice_call_provider import VoiceCallError

SETTINGS = SarvamCallSettings(
    api_key="sk-test",
    org_id="ORG",
    workspace_id="WS",
    app_id="Conversatio-4180004d-2122",
    app_version=4,
    connection_id="Plivo-DevOp-4e558389-fc26",
    agent_phone_number="+918065589544",
    webhook_url="https://api.example/api/sarvam/webhook/tok",
)


def provider() -> SarvamCallProvider:
    return SarvamCallProvider(SETTINGS, base_url="https://apps.sarvam.ai/api")


def payload(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "screening_call_id": "SC-1",
        "candidate_name": "Tanya Jalgaonkar",
        "job_title": "PR Executive",
        "questions": [{"text": "Do you have 2+ years experience?", "type": "yesno"}],
    }
    args.update(over)
    return provider().build_payload(**args)


# -- the agent's contract ----------------------------------------------------


def test_the_variable_names_are_the_ones_the_agent_declares() -> None:
    # Read from the committed config of Conversatio-4180004d-2122. Renaming
    # any of these silently stops the agent being told anything.
    variables = payload()["app_config"]["agent_variables"]
    assert "user_name" in variables
    assert "job_title" in variables
    assert "questions" in variables
    assert "candidate_name" not in variables, "the agent calls this user_name"


def test_the_candidate_is_passed_by_first_name() -> None:
    # The agent greets them with it and addresses them as "{{user_name}} ji"
    # in Hindi, so a full name would read wrong out loud.
    assert payload()["app_config"]["agent_variables"]["user_name"] == "Tanya"


def test_a_missing_name_does_not_become_an_empty_greeting() -> None:
    assert payload(candidate_name="  ")["app_config"]["agent_variables"]["user_name"] == "there"


def test_the_outputs_the_agent_requires_are_sent_empty() -> None:
    # The agent marks these needs_initial_value; they come back filled.
    variables = payload()["app_config"]["agent_variables"]
    for name in ("call_summary", "screening_status", "q1_answer", "q5_followup"):
        assert variables[name] == "", name


def test_the_version_is_pinned_and_never_the_draft() -> None:
    app_config = payload()["app_config"]
    assert app_config["app_version"] == 4
    assert app_config["app_type"] == "agent"


def test_the_record_id_travels_in_metadata_not_in_a_variable() -> None:
    # Metadata is echoed back verbatim; a variable can be rewritten mid-call.
    built = payload()
    assert built["webhook_config"]["metadata"] == {"screeningCallId": "SC-1"}
    assert "screeningCallId" not in built["app_config"]["agent_variables"]


# -- the question list -------------------------------------------------------


def test_questions_are_one_numbered_line_each() -> None:
    text = format_questions([{"text": "First?"}, {"text": "Second?"}])
    assert text == "1. First?\n2. Second?"


def test_questions_is_a_string_not_a_list() -> None:
    # The agent reads it straight into its prompt.
    assert isinstance(payload()["app_config"]["agent_variables"]["questions"], str)


def test_a_choice_question_carries_its_options_in_the_tag_the_agent_knows() -> None:
    text = format_questions([{"text": "Which tools?", "options": ["Canva", "Figma"]}])
    assert text == "1. Which tools? (options: Canva / Figma)"


def test_a_question_spanning_lines_is_flattened() -> None:
    # One question per line is how the agent numbers them; an embedded newline
    # would split one question into two and shift every number after it.
    text = format_questions([{"text": "Do you\nuse  Instagram?"}, {"text": "Second?"}])
    assert text == "1. Do you use Instagram?\n2. Second?"


def test_blank_questions_do_not_leave_a_gap_in_the_numbering() -> None:
    text = format_questions([{"text": "First?"}, {"text": "   "}, {"text": "Third?"}])
    assert text == "1. First?\n2. Third?"


def test_more_questions_than_the_agent_can_answer_are_dropped() -> None:
    # It declares q1..q10 outputs; an eleventh would be asked and then have
    # nowhere to land.
    text = format_questions([{"text": f"Q{n}?"} for n in range(1, 15)])
    assert len(text.splitlines()) == MAX_QUESTIONS
    assert text.splitlines()[-1] == "10. Q10?"


# -- placing the call --------------------------------------------------------


class _Transport:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.sent: dict[str, Any] | None = None
        self.path: str | None = None

    def __call__(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.path, self.sent = path, body
        return self.answer


def test_a_placed_call_returns_the_attempt_id_as_the_provider_id() -> None:
    p = provider()
    transport = _Transport({"attempt_id": "att-123"})
    p._post = transport  # type: ignore[assignment]
    created = p.create_phone_call(payload(), "+919876543210")
    assert created.provider_call_id == "att-123"
    assert created.web_call_url is None


def test_the_destination_number_is_sent_as_user_config() -> None:
    p = provider()
    transport = _Transport({"attempt_id": "att-1"})
    p._post = transport  # type: ignore[assignment]
    p.create_phone_call(payload(), "+919876543210")
    assert transport.sent is not None
    assert transport.sent["user_config"] == {"user_phone_number": "+919876543210"}
    assert transport.path == "/outbounds/v1/orgs/ORG/workspaces/WS/outbounds"


def test_an_answer_without_an_attempt_id_is_a_failure_not_a_call() -> None:
    p = provider()
    p._post = _Transport({})  # type: ignore[assignment]
    with pytest.raises(VoiceCallError):
        p.create_phone_call(payload(), "+919876543210")


def test_there_is_no_browser_call() -> None:
    # Instant Outbound dials a telephone. Saying so here is better than
    # failing somewhere the caller cannot explain to HR.
    with pytest.raises(VoiceCallError, match="phone calls only"):
        provider().create_web_call(payload())


def test_the_whole_body_matches_the_documented_schema() -> None:
    p = provider()
    transport = _Transport({"attempt_id": "att-1"})
    p._post = transport  # type: ignore[assignment]
    p.create_phone_call(payload(), "+919876543210")
    body = transport.sent
    assert body is not None
    assert set(body) == {"app_config", "webhook_config", "user_config"}
    assert set(body["app_config"]) == {
        "app_type", "app_id", "app_version", "connection_config", "agent_variables",
    }
    assert set(body["app_config"]["connection_config"]) == {
        "connection_id", "agent_phone_number",
    }
    json.dumps(body)  # must be serialisable as sent
