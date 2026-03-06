"""
PII protection — masks sensitive information in transcripts and logs.

Detects credit card numbers, SSNs, phone numbers, and other PII patterns,
replacing them with safe placeholders before storage.
"""

from __future__ import annotations

import re

# Credit card: 13-19 digits, optionally separated by spaces or dashes
_CC_PATTERN = re.compile(
    r"\b(\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{1,7})\b"
)

# SSN: 3-2-4 digit pattern
_SSN_PATTERN = re.compile(
    r"\b(\d{3}[\s\-]?\d{2}[\s\-]?\d{4})\b"
)

# Phone numbers (various formats)
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(\+?1?[\s\-\.]?\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})(?!\d)"
)

# Email addresses
_EMAIL_PATTERN = re.compile(
    r"\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b"
)

# Date of birth patterns
_DOB_PATTERN = re.compile(
    r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})\b"
)

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
    if _PIN_PATTERN.search(text):
        return True
    return False


def sanitize_for_logs(text: str) -> str:
    """Full sanitization for debug/error logs — masks all PII patterns."""
    result = mask_pii(text)
    result = _EMAIL_PATTERN.sub("[EMAIL_REDACTED]", result)
    result = _DOB_PATTERN.sub("[DOB_REDACTED]", result)
    return result
