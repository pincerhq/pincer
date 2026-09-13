"""Twilio failures reach the owner as sentences, not as terminal output.

`TwilioRestException.__str__` renders a multi-line, ANSI-coloured block
whenever `sys.stderr` is a tty — which it is whenever the server runs in a
terminal. The dashboard shows the API's `detail` verbatim, so that block used
to arrive in the browser and paint `[31m[49m` across the call composer.

twilio-python's own source flags the hazard next to that formatting: "someone
might catch this error and try to display the message from it to an end user."
`outbound.twilio_error_text` is the answer — it reads the exception's fields
instead of its `__str__`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from twilio.base.exceptions import TwilioRestException

from pincer.voice.outbound import twilio_error_text

#: The real failure from a misconfigured `from` number (Twilio 21210).
UNVERIFIED_FROM = (
    "Unable to create record: The source phone number provided, +19804584570, "
    "is not yet verified for your account. You may only make calls from phone "
    "numbers that you've verified or purchased from Twilio."
)


class _FakeTTY:
    """stderr as the dev server has it, which is what triggers the colouring."""

    def isatty(self) -> bool:
        return True

    def write(self, *_: object) -> None:
        pass

    def flush(self) -> None:
        pass


@contextmanager
def tty_stderr() -> Iterator[None]:
    """Force the colouring branch on.

    Set inside the test body, not in a fixture: pytest installs its output
    capture *after* fixtures resolve and replaces `sys.stderr` with a non-tty,
    so a fixture-level patch is silently undone before the assertion runs.
    """
    real = sys.stderr
    sys.stderr = _FakeTTY()  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stderr = real


def _exc() -> TwilioRestException:
    return TwilioRestException(
        status=400,
        uri="/Accounts/ACxxxx/Calls.json",
        msg=UNVERIFIED_FROM,
        code=21210,
        method="POST",
    )


def test_str_really_does_carry_ansi_on_a_tty() -> None:
    """The premise, pinned: without the helper there is nothing to strip later."""
    with tty_stderr():
        rendered = str(_exc())
    assert "\x1b[31m" in rendered


def test_keeps_twilio_words_and_drops_the_escape_codes() -> None:
    with tty_stderr():
        text = twilio_error_text(_exc())
    assert "\x1b" not in text
    assert "[31m" not in text
    # The part the owner acts on survives intact.
    assert "+19804584570" in text
    assert "is not yet verified for your account" in text


def test_names_the_error_code_so_the_cause_is_searchable() -> None:
    with tty_stderr():
        text = twilio_error_text(_exc())
    assert "21210" in text
    assert "https://www.twilio.com/docs/errors/21210" in text


def test_is_one_line() -> None:
    """A toast and an inline banner both assume a sentence, not a block."""
    with tty_stderr():
        text = twilio_error_text(_exc())
    assert "\n" not in text


def test_falls_back_to_status_when_twilio_sends_no_code() -> None:
    exc = TwilioRestException(status=500, uri="/Calls.json", msg="Service unavailable")
    assert twilio_error_text(exc) == "Service unavailable (HTTP 500)"


def test_plain_exceptions_are_passed_through_stripped() -> None:
    assert twilio_error_text(RuntimeError("\x1b[31mboom\x1b[0m")) == "boom"


def test_exception_with_no_message_still_yields_something() -> None:
    assert twilio_error_text(RuntimeError("")) == ""


def test_from_number_errors_name_the_setting_that_fixes_them() -> None:
    """Twilio names the number; only we know which env var chose it."""
    text = twilio_error_text(_exc())
    assert "PINCER_TWILIO_PHONE_NUMBER" in text


def test_from_number_errors_name_the_rejecting_account() -> None:
    """The usual cause is credentials for a different Twilio project.

    Twilio echoes the account in the request URI, so the message can say which
    one refused the number instead of leaving it to be guessed.
    """
    text = twilio_error_text(_exc())
    assert "ACxxxx" not in text  # the fixture URI has no real SID
    real = TwilioRestException(
        400,
        "/Accounts/AC" + "a" * 32 + "/Calls.json",
        msg="bad From",
        code=21210,
    )
    assert "account AC" + "a" * 32 in twilio_error_text(real)


def test_falls_back_gracefully_when_the_uri_has_no_account() -> None:
    exc = TwilioRestException(400, "/Calls.json", msg="bad From", code=21210)
    assert "PINCER_TWILIO_ACCOUNT_SID" in twilio_error_text(exc)


@pytest.mark.parametrize("code", [21210, 21212, 21606, 21659])
def test_every_from_number_code_gets_the_hint(code: int) -> None:
    exc = TwilioRestException(400, "/Calls.json", msg="bad From", code=code)
    assert "PINCER_TWILIO_PHONE_NUMBER" in twilio_error_text(exc)


def test_unrelated_codes_do_not_get_the_hint() -> None:
    """A busy callee is not a configuration problem; do not send anyone to .env."""
    exc = TwilioRestException(400, "/Calls.json", msg="Account not active", code=20005)
    text = twilio_error_text(exc)
    assert "PINCER_TWILIO_PHONE_NUMBER" not in text
    assert "Account not active" in text
