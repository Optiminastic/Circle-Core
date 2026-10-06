import asyncio
import base64
import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bridge.config import BridgeSettings
from bridge.main import create_app
from bridge.sarvam import SarvamError

SECRET = "bridge-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}
SETTINGS = BridgeSettings(bridge_secret=SECRET, sarvam_api_key="sarvam-key")
PCM = struct.pack("<3h", 1, 2, 3)


def wav_base64(pcm: bytes, rate: int) -> str:
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    return base64.b64encode(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body).decode()


class FakeStream:
    """Yields one transcript, then either stays open (like a live call) or fails/ends."""

    def __init__(self, after: str = "stay-open") -> None:
        self.after = after
        self.sent: list[bytes] = []

    async def send_pcm(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def transcripts(self) -> AsyncIterator[str]:
        yield "haan main daily use karti hoon"
        if self.after == "fail":
            raise SarvamError("dropped")
        if self.after == "stay-open":
            await asyncio.Event().wait()


def make_client(
    sarvam_status: int = 200, stream: FakeStream | None = None, wav_rate: int | None = None
) -> tuple[TestClient, list[Any]]:
    tts_requests: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tts_requests.append(request)
        rate = wav_rate or json.loads(request.content)["speech_sample_rate"]
        return httpx.Response(sarvam_status, json={"audios": [wav_base64(PCM, rate)]})

    @asynccontextmanager
    async def open_stream(settings: BridgeSettings, sample_rate: int) -> AsyncIterator[FakeStream]:
        yield stream or FakeStream()

    app = create_app(
        settings=SETTINGS,
        open_stream=open_stream,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return TestClient(app), tts_requests


def voice_request(text: str = "Hello", sample_rate: int = 16000) -> dict[str, Any]:
    return {"message": {"type": "voice-request", "text": text, "sampleRate": sample_rate}}


def test_health() -> None:
    client, _ = make_client()
    with client:
        assert client.get("/health").json() == {"ok": True, "configured": True}


def test_synthesize_requires_bearer() -> None:
    client, requests = make_client()
    with client:
        assert client.post("/synthesize", json=voice_request()).status_code == 401
        wrong = {"Authorization": "Bearer nope"}
        assert client.post("/synthesize", json=voice_request(), headers=wrong).status_code == 401
    assert requests == []


def test_synthesize_returns_raw_pcm_at_requested_rate() -> None:
    client, requests = make_client()
    with client:
        response = client.post("/synthesize", json=voice_request(sample_rate=24000), headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == PCM
    sent = json.loads(requests[0].content)
    assert sent["speech_sample_rate"] == 24000
    assert requests[0].headers["api-subscription-key"] == "sarvam-key"


@pytest.mark.parametrize(
    "body",
    [voice_request(sample_rate=44100), voice_request(text=" "), {"message": {"type": "other"}}, {}],
)
def test_synthesize_rejects_bad_requests(body: dict[str, Any]) -> None:
    client, _ = make_client()
    with client:
        assert client.post("/synthesize", json=body, headers=AUTH).status_code == 422


def test_synthesize_sarvam_failure_is_502() -> None:
    client, _ = make_client(sarvam_status=500)
    with client:
        assert client.post("/synthesize", json=voice_request(), headers=AUTH).status_code == 502


def test_transcriber_rejects_missing_bearer() -> None:
    client, _ = make_client()
    with client, pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/transcriber") as ws:
            ws.receive_text()
    assert info.value.code == 1008


def test_transcriber_sends_customer_channel_and_relays_transcripts() -> None:
    stream = FakeStream()
    client, _ = make_client(stream=stream)
    with client, client.websocket_connect("/transcriber", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000, "channels": 2}))
        reply = json.loads(ws.receive_text())
        ws.close()  # like Vapi hanging up; the test client otherwise cancels the app
    assert reply == {
        "type": "transcriber-response",
        "transcription": "haan main daily use karti hoon",
        "channel": "customer",
        "transcriptType": "final",
    }


NON_ASCII_AUTH = {"Authorization": "Bearer été".encode("latin-1")}


def test_non_ascii_bearer_is_rejected_cleanly() -> None:
    client, _ = make_client()
    with client:
        assert client.post("/synthesize", json=voice_request(), headers=NON_ASCII_AUTH).status_code == 401
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect("/transcriber", headers=NON_ASCII_AUTH) as ws:
                ws.receive_text()
    assert info.value.code == 1008


def test_synthesize_malformed_body_is_422() -> None:
    client, _ = make_client()
    with client:
        response = client.post("/synthesize", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    assert response.status_code == 422


def test_synthesize_refuses_audio_at_the_wrong_rate() -> None:
    client, _ = make_client(wav_rate=22050)
    with client:
        assert client.post("/synthesize", json=voice_request(sample_rate=16000), headers=AUTH).status_code == 502


def test_transcriber_forwards_only_the_customer_channel() -> None:
    stream = FakeStream()
    client, _ = make_client(stream=stream)
    stereo = struct.pack("<4h", 7, -7, 8, -8) * 800  # 100 ms of 16 kHz stereo
    with client, client.websocket_connect("/transcriber", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000, "channels": 2}))
        ws.receive_text()
        ws.send_bytes(stereo)
        ws.send_bytes(stereo)
        ws.close()
    sent = b"".join(stream.sent)
    assert sent and sent == struct.pack("<2h", 7, 8) * (len(sent) // 4)


@pytest.mark.parametrize("after", ["fail", "end"])
def test_transcriber_closes_with_error_when_sarvam_drops(after: str) -> None:
    client, _ = make_client(stream=FakeStream(after=after))
    with client, client.websocket_connect("/transcriber", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "start", "sampleRate": 16000, "channels": 2}))
        ws.receive_text()
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_text()
    assert info.value.code == 1011


@pytest.mark.parametrize(
    "first",
    [
        b"binary-first",
        "not json",
        json.dumps([1, 2]),
        json.dumps({"type": "start", "sampleRate": 16000, "channels": 0}),
        json.dumps({"type": "start", "sampleRate": 16000, "channels": 2, "encoding": "mulaw"}),
    ],
)
def test_transcriber_rejects_bad_start_frames(first: Any) -> None:
    client, _ = make_client()
    with client, client.websocket_connect("/transcriber", headers=AUTH) as ws:
        if isinstance(first, bytes):
            ws.send_bytes(first)
        else:
            ws.send_text(first)
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_text()
    assert info.value.code == 1011
