"""Throwaway settings for voice tests that drive a real VoiceChannel.

Since Sprint 9 the channel writes a per-call cost row when a call ends. A
`MagicMock` `db_path` stringifies into a junk filename in the working directory,
so every voice test that reaches call end needs a real path. `apply_test_paths`
is the one place that knows which fields those are.
"""

from __future__ import annotations

import pathlib
import tempfile
from typing import Any

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="pincer-voice-tests-"))


def apply_test_paths(settings: Any, tmp_dir: pathlib.Path | None = None) -> Any:
    """Point a MagicMock settings object at a real throwaway directory.

    Prices are zeroed: these tests assert on call behaviour, not on money, and
    a zero-cost row keeps a stray assertion from depending on list prices.
    """
    root = tmp_dir or _TMP
    settings.db_path = str(root / "voice-test.db")
    settings.data_dir = root
    settings.voice_max_call_duration = 600
    settings.alert_stuck_call_grace_s = 60
    settings.price_twilio_outbound_per_min = 0.0
    settings.price_twilio_inbound_per_min = 0.0
    settings.price_conversationrelay_per_min = 0.0
    settings.price_deepgram_per_min = 0.0
    settings.price_elevenlabs_per_1k_chars = 0.0
    # Sprint 13: every call start now resolves its thread. A MagicMock
    # `thread_match_window_days` coerces to 1 and a MagicMock
    # `thread_inbound_context` stringifies to junk, so both are set for real —
    # the defaults here are the shipped defaults (no matching surprises, and
    # `off` means a matched inbound call still speaks nothing about it).
    settings.thread_match_window_days = 7
    settings.thread_inbound_context = "off"
    settings.thread_autoclose_days = 30
    settings.dashboard_url = ""
    return settings


def apply_in_call_tool_defaults(settings: Any, **overrides: Any) -> Any:
    """Sprint 11: explicit in-call tool settings on a MagicMock settings object.

    MagicMock attributes coerce to ``int(...) == 1`` / ``str(...)`` garbage,
    which would silently turn the write budget into 1 and the tool timeout
    into 1s — so every voice test that reaches the gate sets these for real.
    """
    values: dict[str, Any] = {
        "voice_tool_approval": "verbal",
        "voice_tool_approval_overrides": "",
        "voice_tool_timeout_s": 10,
        "voice_approval_timeout_s": 25,
        "voice_max_writes_per_call": 3,
        "voice_tools_extra": "",
        "default_user_id": "",
    }
    values.update(overrides)
    for key, value in values.items():
        setattr(settings, key, value)
    return settings
