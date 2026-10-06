import io
import json
import urllib.error
from typing import Any

import pytest

from app.services import vapi
from app.services.vapi import VapiClient
from app.services.voice_call_provider import VoiceCallError


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    requests: list[Any] = []
    reply: dict[str, Any] = {"id": "call_1", "webCallUrl": "https://vapi.daily.co/room"}

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        assert timeout > 0
        requests.append(request)
        return _Response(json.dumps(reply).encode())

    monkeypatch.setattr(vapi.urllib.request, "urlopen", fake_urlopen)
    return requests


def client(phone_number_id: str = "pn_1") -> VapiClient:
    return VapiClient(api_key="private", public_key="public", phone_number_id=phone_number_id,
                      base_url="https://api.vapi.ai/")


def test_phone_call_uses_private_key_and_number(sent: list[Any]) -> None:
    created = client().create_phone_call({"name": "a"}, "+919876543210")
    request = sent[0]
    assert request.full_url == "https://api.vapi.ai/call"
    assert request.get_header("Authorization") == "Bearer private"
    assert json.loads(request.data) == {
        "assistant": {"name": "a"}, "phoneNumberId": "pn_1", "customer": {"number": "+919876543210"}}
    assert created.provider_call_id == "call_1"
    assert created.web_call_url is None


def test_web_call_uses_public_key_and_returns_link(sent: list[Any]) -> None:
    created = client().create_web_call({"name": "a"})
    assert sent[0].full_url == "https://api.vapi.ai/call/web"
    assert sent[0].get_header("Authorization") == "Bearer public"
    assert created.web_call_url == "https://vapi.daily.co/room"


def test_phone_call_without_number_is_refused_before_any_request(sent: list[Any]) -> None:
    with pytest.raises(VoiceCallError):
        client(phone_number_id="").create_phone_call({}, "+919876543210")
    assert sent == []


def test_http_error_becomes_voice_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 400, "Bad", {}, io.BytesIO(b'{"message":"bad"}'))

    monkeypatch.setattr(vapi.urllib.request, "urlopen", failing)
    with pytest.raises(VoiceCallError) as info:
        client().create_web_call({})
    assert info.value.status == 400


def test_missing_call_id_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vapi.urllib.request, "urlopen", lambda r, timeout: _Response(b"{}"))
    with pytest.raises(VoiceCallError):
        client().create_phone_call({}, "+919876543210")


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), ConnectionResetError("reset")])
def test_timeouts_and_resets_become_voice_call_error(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    def failing(request: Any, timeout: float) -> Any:
        raise error

    monkeypatch.setattr(vapi.urllib.request, "urlopen", failing)
    with pytest.raises(VoiceCallError):
        client().create_web_call({})
