"""Raw PCM helpers. Pure functions over bytes, no I/O."""

from __future__ import annotations

import struct
import sys
from array import array

SAMPLE_WIDTH_BYTES = 2  # 16-bit signed little-endian
_RIFF_HEADER_BYTES = 12
_CHUNK_HEADER_BYTES = 8
_PCM_FORMAT = 1


class AudioFormatError(ValueError):
    """Audio bytes are not in the shape we expect."""


def channel_from_interleaved(pcm: bytes, channel: int, channels: int) -> bytes:
    """Pick one channel out of interleaved 16-bit PCM (Vapi sends caller on 0)."""
    if not 0 <= channel < channels:
        raise AudioFormatError("Channel out of range")
    frame = SAMPLE_WIDTH_BYTES * channels
    samples = array("h", pcm[: len(pcm) - len(pcm) % frame])
    if sys.byteorder != "little":
        samples.byteswap()
    picked = samples[channel::channels]
    if sys.byteorder != "little":
        picked.byteswap()
    return picked.tobytes()


def pcm_from_wav(wav: bytes) -> bytes:
    """Return the sample data of a mono 16-bit PCM WAV, without its header."""
    if len(wav) < _RIFF_HEADER_BYTES or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise AudioFormatError("Not a WAV file")
    offset = _RIFF_HEADER_BYTES
    format_ok = False
    while offset + _CHUNK_HEADER_BYTES <= len(wav):
        chunk_id = wav[offset : offset + 4]
        (size,) = struct.unpack("<I", wav[offset + 4 : offset + 8])
        body = offset + _CHUNK_HEADER_BYTES
        if chunk_id == b"fmt ":
            audio_format, channels, _rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", wav[body : body + 16])
            format_ok = audio_format == _PCM_FORMAT and channels == 1 and bits == SAMPLE_WIDTH_BYTES * 8
        elif chunk_id == b"data":
            if not format_ok:
                raise AudioFormatError("WAV is not mono 16-bit PCM")
            # Streamed WAVs may carry a placeholder size; never read past the end.
            return wav[body : min(body + size, len(wav))]
        offset = body + size + (size % 2)  # chunks are word-aligned
    raise AudioFormatError("WAV has no data chunk")
