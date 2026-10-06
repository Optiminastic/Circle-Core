import struct

import pytest

from bridge.audio import AudioFormatError, channel_from_interleaved, pcm_from_wav


def samples(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def wav(pcm: bytes, *, channels: int = 1, bits: int = 16, extra_chunk: bool = False) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, channels, 16000, 16000 * channels * bits // 8, channels * bits // 8, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra_chunk:
        chunks += b"LIST" + struct.pack("<I", 3) + b"abc" + b"\x00"  # odd size, padded
    chunks += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def test_picks_one_channel_from_stereo() -> None:
    stereo = samples(1, -1, 2, -2, 3, -3)
    assert channel_from_interleaved(stereo, 0, 2) == samples(1, 2, 3)
    assert channel_from_interleaved(stereo, 1, 2) == samples(-1, -2, -3)


def test_drops_a_trailing_partial_frame() -> None:
    assert channel_from_interleaved(samples(1, -1, 2), 0, 2) == samples(1)


def test_rejects_bad_channel() -> None:
    with pytest.raises(AudioFormatError):
        channel_from_interleaved(samples(1, 2), 2, 2)


def test_strips_wav_header_and_skips_other_chunks() -> None:
    pcm = samples(10, 20, 30)
    assert pcm_from_wav(wav(pcm)) == pcm
    assert pcm_from_wav(wav(pcm, extra_chunk=True)) == pcm


def test_rejects_stereo_or_8_bit_wav() -> None:
    with pytest.raises(AudioFormatError):
        pcm_from_wav(wav(samples(1, 2), channels=2))
    with pytest.raises(AudioFormatError):
        pcm_from_wav(wav(b"\x01\x02", bits=8))


def test_rejects_non_wav() -> None:
    with pytest.raises(AudioFormatError):
        pcm_from_wav(b"not a wav file at all")


def test_rejects_wav_at_an_unexpected_rate() -> None:
    pcm = samples(1, 2)
    assert pcm_from_wav(wav(pcm), expected_rate=16000) == pcm
    with pytest.raises(AudioFormatError):
        pcm_from_wav(wav(pcm), expected_rate=24000)
