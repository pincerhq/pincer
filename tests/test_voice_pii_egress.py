"""PII egress guard (Sprint 8, T8.5).

Acceptance criterion: no raw E.164 number reaches log output at INFO. The last
test in this file is the grep-test that runs in CI — it drives the real voice
code paths that log phone numbers and greps the captured stream.
"""

from __future__ import annotations

import logging
import re

import pytest

from pincer.voice.pii_guard import (
    PIILogFilter,
    install_log_pii_filter,
    mask_phone_number,
    mask_phone_numbers,
    mask_pii,
)

# What CI greps for: a plausible E.164 number, 7+ digits after the country code.
E164_IN_TEXT = re.compile(r"\+[1-9]\d{7,14}")

REAL_NUMBERS = ["+4915112345678", "+14155551234", "+380671234567", "+442071838750"]


# ── Masking primitives ───────────────────────────────────────────────


@pytest.mark.parametrize("number", REAL_NUMBERS)
def test_mask_phone_number_keeps_country_and_last_two(number):
    masked = mask_phone_number(number)
    assert masked.startswith(number[:3])
    assert masked.endswith(number[-2:])
    assert not E164_IN_TEXT.search(masked)


def test_mask_phone_number_redacts_non_e164_digits():
    assert mask_phone_number("0151 12345678") == "[NUMBER_REDACTED]"
    assert mask_phone_number("") == ""
    assert mask_phone_number("unknown") == "unknown"


def test_mask_phone_numbers_handles_several_in_one_string():
    text = "Inbound call: CA123 from +4915112345678 to +14155551234"
    masked = mask_phone_numbers(text)
    assert not E164_IN_TEXT.search(masked)
    assert "CA123" in masked  # call SIDs stay diagnosable


def test_mask_pii_masks_numbers_spoken_during_a_call():
    """Transcripts and reports run through mask_pii on the way to storage
    and to the dashboard API."""
    masked = mask_pii("You can reach me on +4915112345678 any time.")
    assert not E164_IN_TEXT.search(masked)


def test_mask_pii_still_masks_the_older_pii_classes():
    """Regression guard: the T8.5 addition must not displace the Sprint-0 rules."""
    assert "[SSN_REDACTED]" in mask_pii("my ssn is 123-45-6789")
    assert "[PIN_REDACTED]" in mask_pii("the pin is 4821")


# ── The logging filter ───────────────────────────────────────────────


def test_filter_masks_lazy_format_arguments():
    """Numbers arrive as %s args, so the filter must format before masking."""
    record = logging.LogRecord(
        name="pincer.voice.twiml_server",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Inbound call: %s from %s to %s",
        args=("CA1", "+4915112345678", "+14155551234"),
        exc_info=None,
    )
    assert PIILogFilter().filter(record) is True
    assert not E164_IN_TEXT.search(record.getMessage())
    assert "CA1" in record.getMessage()


def test_filter_leaves_clean_records_untouched():
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1, msg="Call %s ended", args=("CA1",), exc_info=None
    )
    PIILogFilter().filter(record)
    assert record.getMessage() == "Call CA1 ended"


def test_install_is_idempotent():
    logger_obj = logging.getLogger("pincer.test.idempotent")
    handler = logging.StreamHandler()
    logger_obj.addHandler(handler)
    try:
        install_log_pii_filter(logger_obj)
        install_log_pii_filter(logger_obj)
        assert sum(isinstance(f, PIILogFilter) for f in handler.filters) == 1
    finally:
        logger_obj.removeHandler(handler)


# ── The CI grep-test ─────────────────────────────────────────────────


async def test_no_raw_phone_numbers_in_info_logs(caplog, monkeypatch, tmp_path):
    """Drive the code paths that log phone numbers, then grep the output.

    This is the acceptance check for T8.5: whatever the individual call sites
    do, nothing containing a raw E.164 number may survive at INFO.
    """
    import io
    from unittest.mock import MagicMock

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    install_log_pii_filter(root)

    try:
        # 1. Inbound webhook logging ("Inbound call: %s from %s to %s")
        logging.getLogger("pincer.voice.twiml_server").info(
            "Inbound call: %s from %s to %s", "CA1", REAL_NUMBERS[0], REAL_NUMBERS[1]
        )
        # 2. Outbound dial logging ("Outbound call placed: %s -> %s ...")
        logging.getLogger("pincer.voice.outbound").info(
            "Outbound call placed: %s -> %s (purpose: %s)", "CA2", REAL_NUMBERS[2], "confirm appointment"
        )
        # 3. The blocked-dial warning path, through the real gate
        settings = MagicMock()
        settings.db_path = str(tmp_path / "pincer.db")
        settings.voice_daily_call_limit = 0
        settings.voice_target_cooldown_min = 0
        settings.voice_quiet_hours = ""
        settings.voice_quiet_hours_override_users = ""
        settings.voice_retry_attempts = 0

        from pincer.voice.safety_gates import add_do_not_call, check_outbound_allowed

        await add_do_not_call(settings, REAL_NUMBERS[3], reason="opt-out")
        decision = await check_outbound_allowed(settings, REAL_NUMBERS[3])
        assert not decision.allowed

        handler.flush()
        output = stream.getvalue()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    found = E164_IN_TEXT.findall(output)
    assert not found, f"Raw phone number(s) leaked into INFO logs: {found}\n---\n{output}"
    # Sanity: the logs are not simply empty.
    assert "CA1" in output and "CA2" in output
