"""Tests for audio codec conversion utilities."""

from __future__ import annotations

import struct

from pincer.voice.audio import (
    mulaw8k_to_pcm16k,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    pcm16k_to_mulaw8k,
    resample_8k_to_16k,
    resample_16k_to_8k,
)


class TestMulawConversion:
    def test_encode_silence(self):
        pcm = struct.pack("<4h", 0, 0, 0, 0)
        mulaw = pcm16_to_mulaw(pcm)
        assert len(mulaw) == 4

    def test_round_trip(self):
        """Encode then decode should produce approximately the same signal."""
        original_samples = [0, 1000, -1000, 5000, -5000, 16000, -16000]
        pcm = struct.pack(f"<{len(original_samples)}h", *original_samples)
        mulaw = pcm16_to_mulaw(pcm)
        decoded = mulaw_to_pcm16(mulaw)
        decoded_samples = struct.unpack(f"<{len(original_samples)}h", decoded)

        for orig, dec in zip(original_samples, decoded_samples, strict=False):
            if orig == 0:
                assert abs(dec) < 200
            else:
                error_ratio = abs(dec - orig) / max(abs(orig), 1)
                assert error_ratio < 0.15, f"orig={orig}, dec={dec}, ratio={error_ratio}"

    def test_output_length(self):
        pcm = struct.pack("<100h", *range(100))
        mulaw = pcm16_to_mulaw(pcm)
        assert len(mulaw) == 100  # 1 byte per sample


class TestResampling:
    def test_upsample_doubles_length(self):
        pcm_8k = struct.pack("<10h", *range(10))
        pcm_16k = resample_8k_to_16k(pcm_8k)
        n_out = len(pcm_16k) // 2
        assert n_out == 20  # 10 samples -> 20 samples

    def test_downsample_halves_length(self):
        pcm_16k = struct.pack("<20h", *range(20))
        pcm_8k = resample_16k_to_8k(pcm_16k)
        n_out = len(pcm_8k) // 2
        assert n_out == 10  # 20 samples -> 10 samples

    def test_downsample_preserves_even_samples(self):
        samples = list(range(0, 200, 10))
        pcm_16k = struct.pack(f"<{len(samples)}h", *samples)
        pcm_8k = resample_16k_to_8k(pcm_16k)
        downsampled = struct.unpack(f"<{len(pcm_8k) // 2}h", pcm_8k)
        assert downsampled == tuple(samples[::2])


class TestFullPipeline:
    def test_mulaw8k_to_pcm16k(self):
        pcm_8k = struct.pack("<50h", *[i * 100 for i in range(50)])
        mulaw = pcm16_to_mulaw(pcm_8k)
        pcm_16k = mulaw8k_to_pcm16k(mulaw)
        n_out = len(pcm_16k) // 2
        assert n_out == 100  # 50 * 2 = 100

    def test_pcm16k_to_mulaw8k(self):
        pcm_16k = struct.pack("<100h", *[i * 50 for i in range(100)])
        mulaw = pcm16k_to_mulaw8k(pcm_16k)
        assert len(mulaw) == 50  # 100 / 2 = 50

    def test_round_trip_pipeline(self):
        """Full pipeline: PCM 16kHz -> mu-law 8kHz -> PCM 16kHz."""
        original = [0, 500, -500, 2000, -2000, 8000, -8000]
        n = len(original) * 2
        pcm_16k = struct.pack(f"<{n}h", *[s for s in original for _ in range(2)])
        mulaw = pcm16k_to_mulaw8k(pcm_16k)
        recovered = mulaw8k_to_pcm16k(mulaw)
        assert len(recovered) == len(pcm_16k)

    def test_empty_input(self):
        assert pcm16_to_mulaw(b"") == b""
        assert mulaw_to_pcm16(b"") == b""
