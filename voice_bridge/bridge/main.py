"""Sarvam voice bridge for Vapi.

Vapi's custom-voice provider POSTs text to /synthesize and gets raw PCM back;
its custom-transcriber provider streams call audio to /transcriber and gets
transcripts back. Both require `Authorization: Bearer <VOICE_BRIDGE_SECRET>`.
Never logs transcripts or spoken text: they are candidate PII.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status

from bridge.audio import channel_from_interleaved
from bridge.auth import is_authorized
from bridge.config import BridgeSettings, load_settings
from bridge.sarvam import SarvamError, TranscriptStream, open_transcript_stream, synthesize

logger = logging.getLogger("voice_bridge")

VAPI_TTS_SAMPLE_RATES = (8000, 16000, 22050, 24000)
CUSTOMER_CHANNEL = 0
# Batch Vapi's ~20 ms frames into ~100 ms chunks before sending to Sarvam.
CHUNK_SECONDS = 0.1
POLICY_VIOLATION = 1008
START_FRAME_TIMEOUT_SECONDS = 10
MAX_CHANNELS = 2

StreamOpener = Callable[[BridgeSettings, int], AbstractAsyncContextManager[TranscriptStream]]


def create_app(
    settings: BridgeSettings | None = None,
    open_stream: StreamOpener = open_transcript_stream,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    config = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = http_client or httpx.AsyncClient()
        app.state.http = client
        yield
        if http_client is None:
            await client.aclose()

    app = FastAPI(title="Circle voice bridge", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True, "configured": config.configured}

    @app.post("/synthesize")
    async def synthesize_speech(request: Request, authorization: str | None = Header(default=None)) -> Response:
        if not is_authorized(authorization, config.bridge_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Body is not JSON") from exc
        text, sample_rate = _voice_request(body)
        try:
            pcm = await synthesize(request.app.state.http, config, text, sample_rate)
        except SarvamError as exc:
            logger.warning("Text-to-speech failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Speech synthesis failed") from exc
        return Response(content=pcm, media_type="application/octet-stream")

    @app.websocket("/transcriber")
    async def transcriber(websocket: WebSocket) -> None:
        if not is_authorized(websocket.headers.get("authorization"), config.bridge_secret):
            await websocket.close(code=POLICY_VIOLATION)
            return
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive(), START_FRAME_TIMEOUT_SECONDS)
            sample_rate, channels = _start_frame(first)
            async with open_stream(config, sample_rate) as stream:
                await _relay(websocket, stream, sample_rate, channels)
        except WebSocketDisconnect:
            pass
        except (SarvamError, ValueError, TimeoutError) as exc:
            logger.warning("Transcriber session ended with error: %s", type(exc).__name__)
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)

    return app


def _voice_request(body: Any) -> tuple[str, int]:
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, dict) or message.get("type") != "voice-request":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Expected a voice-request")
    text = message.get("text")
    sample_rate = message.get("sampleRate")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing text")
    if sample_rate not in VAPI_TTS_SAMPLE_RATES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported sample rate")
    return text, sample_rate


def _start_frame(message: dict[str, Any]) -> tuple[int, int]:
    """Validate Vapi's first frame: {"type": "start", "sampleRate": ..., "channels": ...}."""
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect()
    try:
        start = json.loads(message.get("text") or "")
    except ValueError as exc:
        raise ValueError("First frame is not JSON") from exc
    if not isinstance(start, dict) or start.get("type") != "start":
        raise ValueError("First frame is not a start message")
    if start.get("encoding", "linear16") != "linear16" or start.get("container", "raw") != "raw":
        raise ValueError("Only raw linear16 audio is supported")
    sample_rate, channels = start.get("sampleRate"), start.get("channels", 1)
    if not isinstance(sample_rate, int) or not isinstance(channels, int) or not 1 <= channels <= MAX_CHANNELS:
        raise ValueError("Bad sample rate or channel count")
    return sample_rate, channels


async def _relay(websocket: WebSocket, stream: TranscriptStream, sample_rate: int, channels: int) -> None:
    """Audio Vapi -> Sarvam and transcripts Sarvam -> Vapi until either side stops.

    Both tasks are always cancelled and awaited, even if this coroutine is
    cancelled, so neither can outlive the Sarvam connection it uses.
    """
    pump = asyncio.create_task(_pump_audio(websocket, stream, sample_rate, channels))
    relay = asyncio.create_task(_send_transcripts(websocket, stream))
    try:
        await asyncio.wait({pump, relay}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (pump, relay):
            task.cancel()
        results = await asyncio.gather(pump, relay, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            raise result
    if relay.done() and not relay.cancelled() and not (pump.done() and not pump.cancelled()):
        # Sarvam closed while Vapi was still sending audio: transcription stopped.
        raise SarvamError("Sarvam ended the transcript stream early")


async def _pump_audio(websocket: WebSocket, stream: TranscriptStream, sample_rate: int, channels: int) -> None:
    chunk_bytes = int(sample_rate * CHUNK_SECONDS) * 2
    buffer = bytearray()
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        frame = message.get("bytes")
        if not frame:
            continue  # control text frames after start
        buffer += channel_from_interleaved(frame, CUSTOMER_CHANNEL, channels) if channels > 1 else frame
        if len(buffer) >= chunk_bytes:
            await stream.send_pcm(bytes(buffer))
            buffer.clear()


async def _send_transcripts(websocket: WebSocket, stream: TranscriptStream) -> None:
    async for text in stream.transcripts():
        await websocket.send_text(
            json.dumps(
                {
                    "type": "transcriber-response",
                    "transcription": text,
                    "channel": "customer",
                    "transcriptType": "final",
                }
            )
        )


app = create_app()
