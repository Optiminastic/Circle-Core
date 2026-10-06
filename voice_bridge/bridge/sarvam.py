"""Sarvam API clients: text-to-speech (REST) and streaming speech-to-text (WebSocket).

Hand-rolled instead of the sarvamai SDK because the SDK's streaming helper only
sends WAV, while Vapi gives us raw 16-bit PCM.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import websockets

from bridge.audio import pcm_from_wav
from bridge.config import BridgeSettings

TTS_TIMEOUT_SECONDS = 15
# Sarvam accepts up to 2500 characters per bulbul:v3 request; agent turns are short.
MAX_TTS_CHARS = 2500
STT_SAMPLE_RATES = (8000, 16000)
_API_KEY_HEADER = "api-subscription-key"


class SarvamError(RuntimeError):
    """Sarvam refused or could not be reached."""


async def synthesize(
    http: httpx.AsyncClient, settings: BridgeSettings, text: str, sample_rate: int
) -> bytes:
    """Speak `text` and return mono 16-bit PCM at `sample_rate` (no WAV header)."""
    payload = {
        "text": text[:MAX_TTS_CHARS],
        "language_code": settings.tts_language,
        "speaker": settings.tts_speaker,
        "model": settings.tts_model,
        "speech_sample_rate": sample_rate,
        "output_audio_codec": "wav",
    }
    try:
        response = await http.post(
            f"{settings.sarvam_base_url}/text-to-speech",
            json=payload,
            headers={_API_KEY_HEADER: settings.sarvam_api_key},
            timeout=TTS_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise SarvamError("Could not reach Sarvam text-to-speech") from exc
    if response.status_code != httpx.codes.OK:
        raise SarvamError(f"Sarvam text-to-speech returned {response.status_code}")
    audios = response.json().get("audios") or []
    if not audios:
        raise SarvamError("Sarvam text-to-speech returned no audio")
    return b"".join(pcm_from_wav(base64.b64decode(audio)) for audio in audios)


class TranscriptStream(Protocol):
    async def send_pcm(self, pcm: bytes) -> None: ...

    def transcripts(self) -> AsyncIterator[str]: ...


class SarvamTranscriptStream:
    """One live Sarvam speech-to-text session over a WebSocket."""

    def __init__(self, connection: Any, sample_rate: int) -> None:
        self._connection = connection
        self._sample_rate = str(sample_rate)

    async def send_pcm(self, pcm: bytes) -> None:
        message = {
            "audio": {
                "data": base64.b64encode(pcm).decode(),
                "sample_rate": self._sample_rate,
                "encoding": "audio/wav",
            }
        }
        await self._connection.send(json.dumps(message))

    async def transcripts(self) -> AsyncIterator[str]:
        async for raw in self._connection:
            message = json.loads(raw)
            if message.get("type") == "error":
                raise SarvamError("Sarvam speech-to-text reported an error")
            if message.get("type") != "data":
                continue  # VAD events
            text = (message.get("data") or {}).get("transcript", "").strip()
            if text:
                yield text


@asynccontextmanager
async def open_transcript_stream(settings: BridgeSettings, sample_rate: int) -> AsyncIterator[TranscriptStream]:
    if sample_rate not in STT_SAMPLE_RATES:
        raise SarvamError(f"Unsupported sample rate {sample_rate}")
    query = urlencode(
        {
            "model": settings.stt_model,
            "mode": "transcribe",
            "language-code": settings.stt_language,
            "sample_rate": str(sample_rate),
            "input_audio_codec": "pcm_s16le",
            "high_vad_sensitivity": "true",
            "vad_signals": "true",
        }
    )
    async with websockets.connect(
        f"{settings.sarvam_ws_url}?{query}",
        additional_headers={_API_KEY_HEADER: settings.sarvam_api_key},
    ) as connection:
        yield SarvamTranscriptStream(connection, sample_rate)
