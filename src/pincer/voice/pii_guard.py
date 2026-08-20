"""
PII protection — masks sensitive information in transcripts and logs.

Detects credit card numbers, SSNs, phone numbers, and other PII patterns,
replacing them with safe placeholders before storage.

Sprint 8 (T8.5) adds the log egress guard: `install_log_pii_filter()` attaches
`PIILogFilter` to the root logger so no raw E.164 number ever reaches stdout,
a log file, or a log shipper. Call SIDs, timings, and statuses are untouched —
only the number itself is reduced to `+49…89`, which stays diagnosable
(country code + last two digits) without identifying the person.
"""

from __future__ import annotations

import logging
import re

# Credit card: 13-19 digits, optionally separated by spaces or dashes
_CC_PATTERN = re.compile(r"\b(\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,7})\b")

# SSN: 3-2-4 digit pattern
_SSN_PATTERN = re.compile(r"\b(\d{3}[\s\-]?\d{2}[\s\-]?\d{4})\b")

# Phone numbers (various formats)
_PHONE_PATTERN = re.compile(r"(?<!\d)(\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})(?!\d)")

# Email addresses
_EMAIL_PATTERN = re.compile(r"\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b")

# Date of birth patterns
_DOB_PATTERN = re.compile(r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})\b")

# Account numbers (8+ digits)
_ACCOUNT_PATTERN = re.compile(
    r"\baccount\s*(?:number|#|num)?[\s:]*(\d{8,})\b",
    re.IGNORECASE,
)

# PIN / password patterns
_PIN_PATTERN = re.compile(
    r"\b(?:pin|password|passcode|code)\b[\s:]+(?:\w+\s+)*?(\d{4,8})\b",
    re.IGNORECASE,
)


def mask_pii(text: str) -> str:
    """Mask PII patterns in text for safe storage."""
    result = text

    result = _CC_PATTERN.sub(_mask_credit_card, result)
    result = _SSN_PATTERN.sub("[SSN_REDACTED]", result)
    result = _ACCOUNT_PATTERN.sub(
        lambda m: m.group(0).replace(m.group(1), "[ACCOUNT_REDACTED]"),
        result,
    )
    result = _PIN_PATTERN.sub(
        lambda m: m.group(0).replace(m.group(1), "[PIN_REDACTED]"),
        result,
    )
    # T8.5: E.164 numbers spoken aloud during a call are PII too, and this is
    # the function every transcript/action/report passes through on its way to
    # storage and to the dashboard API.
    result = mask_phone_numbers(result)

    return result


def _mask_credit_card(match: re.Match) -> str:
    """Mask a credit card number, keeping first and last 4 digits."""
    digits = re.sub(r"[\s\-]", "", match.group(1))
    if len(digits) < 13:
        return match.group(0)
    if not _luhn_check(digits):
        return match.group(0)
    return f"{digits[:4]} **** **** {digits[-4:]}"


def _luhn_check(number: str) -> bool:
    """Validate a number using the Luhn algorithm (credit card check)."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13:
        return False

    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_dtmf_input(digits: str) -> str:
    """Mask DTMF input that might contain PINs or account numbers."""
    if len(digits) >= 4:
        return digits[0] + "*" * (len(digits) - 2) + digits[-1]
    return digits


def contains_pii(text: str) -> bool:
    """Check if text contains any PII patterns."""
    if _CC_PATTERN.search(text):
        digits = re.sub(r"[\s\-]", "", _CC_PATTERN.search(text).group(1))
        if len(digits) >= 13 and _luhn_check(digits):
            return True
    if _SSN_PATTERN.search(text):
        return True
    if _ACCOUNT_PATTERN.search(text):
        return True
    return bool(_PIN_PATTERN.search(text))


def sanitize_for_logs(text: str) -> str:
    """Full sanitization for debug/error logs — masks all PII patterns."""
    result = mask_pii(text)
    result = _EMAIL_PATTERN.sub("[EMAIL_REDACTED]", result)
    result = _DOB_PATTERN.sub("[DOB_REDACTED]", result)
    return result


# ── Phone-number egress guard (Sprint 8, T8.5) ───────────────────────

# E.164 as it actually appears in our logs and Twilio payloads: a leading +,
# country code, 6-14 more digits. Deliberately stricter than _PHONE_PATTERN
# (which is US-shaped) so European numbers are caught too.
_E164_PATTERN = re.compile(r"\+[1-9]\d{6,14}\b")


def mask_phone_number(number: str) -> str:
    """Reduce a phone number to a diagnosable, non-identifying form.

    `+4915112345689` -> `+49…89`. Short or non-E.164 values are fully masked
    rather than leaked verbatim.
    """
    raw = (number or "").strip()
    match = _E164_PATTERN.fullmatch(raw) or _E164_PATTERN.match(raw)
    if not match:
        return "[NUMBER_REDACTED]" if any(c.isdigit() for c in raw) else raw
    digits = match.group(0)
    country = digits[:3]  # '+' + up to two country-code digits
    return f"{country}…{digits[-2:]}"


def mask_phone_numbers(text: str) -> str:
    """Mask every E.164 number appearing in free text."""
    return _E164_PATTERN.sub(lambda m: mask_phone_number(m.group(0)), text)


class PIILogFilter(logging.Filter):
    """Root-logger filter that masks phone numbers in every emitted record.

    Formats the record's args into the message before masking, because the
    numbers almost always arrive as lazy `%s` arguments
    (`logger.info("Inbound call: %s from %s", sid, caller)`).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover — malformed record, let it through
            return True
        masked = mask_phone_numbers(message)
        if masked != message:
            record.msg = masked
            record.args = None
        return True


_LOG_FILTER = PIILogFilter()


def install_log_pii_filter(logger_obj: logging.Logger | None = None) -> None:
    """Attach the PII filter to a logger's handlers (root by default).

    Handler-level rather than logger-level: a filter on the root *logger* is
    not consulted for records propagated up from child loggers, which is
    exactly where the phone numbers come from.
    """
    target = logger_obj or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, PIILogFilter) for f in handler.filters):
            handler.addFilter(_LOG_FILTER)
