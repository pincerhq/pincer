"""
Audio codec and format conversion utilities for telephony <-> AI model pipeline.

Handles conversions between Twilio's mu-law 8kHz format and the linear PCM
16kHz format used by STT/TTS providers.
"""

from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

# mu-law encoding tables
_MULAW_BIAS = 0x84
_MULAW_MAX = 0x7FFF
_MULAW_CLIP = 32635

_MULAW_ENCODE_TABLE = [
    0,
    0,
    1,
    1,
    2,
    2,
    2,
    2,
    3,
    3,
    3,
    3,
    3,
    3,
    3,
    3,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    4,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    5,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    6,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
    7,
]


def _encode_mulaw_sample(sample: int) -> int:
    """Encode a single 16-bit PCM sample to mu-law."""
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    sample = min(sample, _MULAW_CLIP)
    sample += _MULAW_BIAS
    exponent = _MULAW_ENCODE_TABLE[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_mulaw_sample(byte: int) -> int:
    """Decode a single mu-law byte to 16-bit PCM."""
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample -= _MULAW_BIAS
    return -sample if sign else sample


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Convert mu-law 8kHz audio to linear PCM 16-bit signed."""
    samples = []
    for byte in mulaw_data:
        samples.append(_decode_mulaw_sample(byte))
    return struct.pack(f"<{len(samples)}h", *samples)


def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    """Convert linear PCM 16-bit signed to mu-law."""
    n_samples = len(pcm_data) // 2
    samples = struct.unpack(f"<{n_samples}h", pcm_data)
    return bytes(_encode_mulaw_sample(s) for s in samples)


def resample_8k_to_16k(pcm_8k: bytes) -> bytes:
    """Upsample PCM from 8kHz to 16kHz using linear interpolation."""
    n_samples = len(pcm_8k) // 2
    if n_samples < 2:
        return pcm_8k
    samples = struct.unpack(f"<{n_samples}h", pcm_8k)
    out = []
    for i in range(n_samples - 1):
        out.append(samples[i])
        mid = (samples[i] + samples[i + 1]) // 2
        out.append(mid)
    out.append(samples[-1])
    out.append(samples[-1])
    return struct.pack(f"<{len(out)}h", *out)


def resample_16k_to_8k(pcm_16k: bytes) -> bytes:
    """Downsample PCM from 16kHz to 8kHz by taking every other sample."""
    n_samples = len(pcm_16k) // 2
    samples = struct.unpack(f"<{n_samples}h", pcm_16k)
    downsampled = samples[::2]
    return struct.pack(f"<{len(downsampled)}h", *downsampled)


def mulaw8k_to_pcm16k(mulaw_data: bytes) -> bytes:
    """Convert Twilio mu-law 8kHz to linear PCM 16kHz for STT providers."""
    pcm_8k = mulaw_to_pcm16(mulaw_data)
    return resample_8k_to_16k(pcm_8k)


def pcm16k_to_mulaw8k(pcm_16k: bytes) -> bytes:
    """Convert TTS PCM 16kHz output to mu-law 8kHz for Twilio."""
    pcm_8k = resample_16k_to_8k(pcm_16k)
    return pcm16_to_mulaw(pcm_8k)
