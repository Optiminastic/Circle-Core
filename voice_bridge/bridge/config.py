"""Bridge settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_STT_MODEL = "saaras:v3"
DEFAULT_TTS_MODEL = "bulbul:v3"


@dataclass(frozen=True)
class BridgeSettings:
    # Bearer token Vapi presents (circle-api's VOICE_BRIDGE_SECRET).
    bridge_secret: str
    sarvam_api_key: str
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_ws_url: str = "wss://api.sarvam.ai/speech-to-text/ws"
    stt_model: str = DEFAULT_STT_MODEL
    # "unknown" lets Sarvam detect Hindi / English / Hinglish per utterance.
    stt_language: str = "unknown"
    tts_model: str = DEFAULT_TTS_MODEL
    tts_language: str = "en-IN"
    tts_speaker: str = "shubh"

    @property
    def configured(self) -> bool:
        return bool(self.bridge_secret.strip() and self.sarvam_api_key.strip())


def load_settings() -> BridgeSettings:
    env = os.environ.get
    return BridgeSettings(
        bridge_secret=env("VOICE_BRIDGE_SECRET", ""),
        sarvam_api_key=env("SARVAM_API_KEY", ""),
        stt_model=env("SARVAM_STT_MODEL", DEFAULT_STT_MODEL),
        stt_language=env("SARVAM_STT_LANGUAGE", "unknown"),
        tts_model=env("SARVAM_TTS_MODEL", DEFAULT_TTS_MODEL),
        tts_language=env("SARVAM_TTS_LANGUAGE", "en-IN"),
        tts_speaker=env("SARVAM_TTS_SPEAKER", "shubh"),
    )
