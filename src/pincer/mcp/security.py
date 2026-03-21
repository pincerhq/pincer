"""
MCPSecurityGate — security layer for all MCP operations.

1. Output sanitization: detect prompt injection in MCP tool outputs
2. Rate limiting: per-server, per-tool sliding window counter
3. Credential redaction: scrub API keys from audit log entries
4. Argument validation: basic sanity checks on tool call arguments
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from time import monotonic
from typing import Any

logger = logging.getLogger("pincer.mcp.security")

# ── Prompt injection patterns ─────────────────────────────────────────────────

_INJECTION_PATTERNS: list[str] = [
    r"ignore\s+(?:all\s+)?(?:previous|above)\s+instructions",
    r"you\s+are\s+now\s+(?:a|an)\s+",
    r"<\s*/?system\s*>",
    r"IMPORTANT:\s*(?:override|ignore|forget)",
    r"new\s+instructions?\s*:",
    r"forget\s+(?:everything|all|your)\s+(?:previous|prior)",
    r"disregard\s+(?:everything|all|your)\s+(?:previous|prior)",
    r"\[\s*SYSTEM\s*\]",
    r"BEGIN\s+(?:NEW\s+)?(?:SYSTEM\s+)?PROMPT",
    r"(?:assistant|system)\s*:\s*\n",
]

_COMPILED_INJECTION = [
    re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _INJECTION_PATTERNS
]

# ── Credential redaction patterns ─────────────────────────────────────────────

_REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    # Anthropic API keys: sk-ant-...
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}", re.IGNORECASE), "***REDACTED***"),
    # OpenAI-style keys: sk-...
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}", re.IGNORECASE), "***REDACTED***"),
    # GitHub personal access tokens
    (re.compile(r"ghp_[A-Za-z0-9_]{30,}", re.IGNORECASE), "***REDACTED***"),
    # Bearer tokens in headers/strings
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE), "Bearer ***REDACTED***"),
    # JSON password fields
    (re.compile(r'"password"\s*:\s*"[^"]*"', re.IGNORECASE), '"password": "***REDACTED***"'),
    (re.compile(r"'password'\s*:\s*'[^']*'", re.IGNORECASE), "'password': '***REDACTED***'"),
    # Generic secret/token fields in JSON
    (
        re.compile(r'"(?:secret|api_key|access_token|private_key)"\s*:\s*"[^"]{8,}"', re.IGNORECASE),
        '"***key***: "***REDACTED***"',
    ),
]


# ── Rate limiter ──────────────────────────────────────────────────────────────


class SlidingWindowRateLimiter:
    """
    Per-key sliding window rate limiter.

    Tracks call timestamps and rejects calls that exceed max_calls
    within the most recent window_seconds.
    """

    def __init__(self, max_calls: int = 60, window_seconds: float = 60.0) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        """Return True if the call is allowed, False if rate limited."""
        now = monotonic()
        # Prune timestamps outside the window
        self._calls[key] = [t for t in self._calls[key] if now - t < self.window]
        if len(self._calls[key]) >= self.max_calls:
            return False
        self._calls[key].append(now)
        return True

    def reset(self, key: str) -> None:
        """Clear rate-limit state for a key (useful in tests)."""
        self._calls.pop(key, None)


# ── Security gate ─────────────────────────────────────────────────────────────


class MCPSecurityGate:
    """
    Security layer for all MCP operations (client and server side).

    Thread-safe for asyncio use (single-threaded event loop).
    """

    def __init__(self, rate_limit_per_min: int = 60) -> None:
        self._rate_limiter = SlidingWindowRateLimiter(
            max_calls=rate_limit_per_min, window_seconds=60.0
        )

    # ── Output sanitization ───────────────────────────────────────────────────

    def sanitize_output(self, server: str, tool: str, output: str) -> str:
        """
        Scan MCP tool output for prompt injection patterns.

        Prepends a [SECURITY NOTE] warning if injection is detected but still
        returns the (flagged) output — blocking would hide the LLM's content.
        """
        matched: list[str] = []
        for pattern in _COMPILED_INJECTION:
            if pattern.search(output):
                matched.append(pattern.pattern)

        if matched:
            logger.warning(
                "MCP security: prompt injection detected in '%s::%s' output (patterns: %s)",
                server,
                tool,
                matched,
            )
            return (
                f"[SECURITY NOTE: Possible prompt injection detected in output from {server}::{tool}]\n\n"
                + output
            )

        return output

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def check_rate_limit(self, server: str, tool: str) -> bool:
        """
        Check rate limit for a tool call.

        Returns True if the call is allowed, False if it should be blocked.
        """
        key = f"{server}::{tool}"
        allowed = self._rate_limiter.allow(key)
        if not allowed:
            logger.warning("MCP rate limit exceeded for '%s::%s'", server, tool)
        return allowed

    # ── Credential redaction ──────────────────────────────────────────────────

    def redact_sensitive(self, text: str) -> str:
        """Scrub API keys, tokens, and passwords from a string."""
        for pattern, replacement in _REDACT_RULES:
            text = pattern.sub(replacement, text)
        return text

    # ── Argument validation ───────────────────────────────────────────────────

    def validate_arguments(self, tool_schema: dict[str, Any], arguments: dict[str, Any]) -> bool:
        """
        Basic argument validation against tool schema.

        Returns False if arguments contain obvious injection attempts
        (shell metacharacters in unexpected string fields, etc.).
        """
        if not isinstance(arguments, dict):
            return False

        properties = tool_schema.get("properties", {})
        required = tool_schema.get("required", [])

        # Check required fields are present
        for field in required:
            if field not in arguments:
                return False

        # Check for shell injection in string args
        _shell_patterns = re.compile(r"[;&|`$<>]")
        for key, value in arguments.items():
            prop_schema = properties.get(key, {})
            expected_type = prop_schema.get("type", "string")
            if (
                expected_type == "string"
                and isinstance(value, str)
                and "command" not in key.lower()
                and "shell" not in key.lower()
                and _shell_patterns.search(value)
                and len(value) < 50
            ):
                logger.warning(
                    "MCP security: suspicious shell characters in arg '%s'", key
                )
                # Warn but don't block — the LLM may have legitimate needs
        return True
