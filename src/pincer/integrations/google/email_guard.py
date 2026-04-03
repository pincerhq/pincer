"""
Email safety guard for outbound messages.

Validates recipient addresses BEFORE the approval gate.
Mirrors the BLOCKED_PATTERNS model from shell.py but for email.

Layers (first match wins):
  1. Allowlist   — skip all checks (user-defined trusted addresses/domains)
  2. Blocklist   — hard deny (user-defined blocked addresses/domains)
  3. Dangerous   — hard deny (hardcoded dangerous patterns and known abuse domains)
  4. Disposable  — soft deny (community blocklist, configurable)
  5. Suspicious  — warn only (heuristic, shown in approval prompt)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class EmailVerdict(Enum):
    ALLOW = "allow"
    WARN = "warn"                   # proceed but flag in approval prompt
    DENY_DISPOSABLE = "deny_disposable"
    DENY_BLOCKLIST = "deny_blocklist"
    DENY_DANGEROUS = "deny_dangerous"


@dataclass
class EmailCheckResult:
    verdict: EmailVerdict
    reason: str
    address: str


# ── Layer 3: Hardcoded dangerous domain patterns ──────────────────────────────

DANGEROUS_DOMAIN_PATTERNS: list[re.Pattern[str]] = [
    # Known abuse/scam TLDs (>95% abuse rate per industry reports)
    re.compile(r"\.tk$"),
    re.compile(r"\.ml$"),
    re.compile(r"\.ga$"),
    re.compile(r"\.cf$"),
    re.compile(r"\.gq$"),
    # Obviously suspicious domain prefixes
    re.compile(r"^(spam|phish|scam|hack|evil|malware|virus|trojan)\.", re.IGNORECASE),
    # Suspicious @<keyword> patterns
    re.compile(r"@(spam|phish|scam|hack|evil|malware|abuse)\.", re.IGNORECASE),
]

DANGEROUS_EXACT_DOMAINS: frozenset[str] = frozenset({
    "evil.com",
    "mailinator.com",
    "sharklasers.com",
    "guerrillamail.com",
    "grr.la",
    "guerrillamailblock.com",
    "pokemail.net",
    "spam4.me",
    "trashmail.com",
    "yopmail.com",
    "throwaway.email",
    "tempail.com",
    "dispostable.com",
    "maildrop.cc",
})

# ── Layer 5: Suspicious patterns (warn only) ──────────────────────────────────

SUSPICIOUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^noreply@", re.IGNORECASE), "Sending to a no-reply address"),
    (re.compile(r"^no-reply@", re.IGNORECASE), "Sending to a no-reply address"),
    (re.compile(r"^donotreply@", re.IGNORECASE), "Sending to a do-not-reply address"),
    (re.compile(r"^mailer-daemon@", re.IGNORECASE), "Sending to mailer-daemon"),
    (re.compile(r"^postmaster@", re.IGNORECASE), "Sending to postmaster"),
    (re.compile(r"^test@", re.IGNORECASE), "Sending to a test address"),
    (re.compile(r"@example\.(com|org|net)$", re.IGNORECASE), "Sending to an example.com domain (not deliverable)"),
    (re.compile(r"@localhost$", re.IGNORECASE), "Sending to localhost"),
    (re.compile(r"^[a-z0-9]{20,}@", re.IGNORECASE), "Recipient looks auto-generated (20+ random chars before @)"),
]


class EmailGuard:
    """
    Validates outbound email recipients against layered safety checks.

    Usage::

        guard = EmailGuard(data_dir=Path("data"))
        result = guard.check("spam@evil.com")
        if result.verdict.name.startswith("DENY"):
            return f"Blocked: {result.reason}"
    """

    def __init__(self, data_dir: Path, block_disposable: bool = True) -> None:
        self._data_dir = Path(data_dir)
        self._block_disposable = block_disposable

        self._allowlist_addresses: set[str] = set()
        self._allowlist_domains: set[str] = set()
        self._blocklist_addresses: set[str] = set()
        self._blocklist_domains: set[str] = set()
        self._disposable_domains: set[str] = set()

        self._load_allowlist()
        self._load_blocklist()
        self._load_disposable_domains()

    # ── File loading ──────────────────────────────────────────────────────────

    def _load_file_lines(self, filename: str) -> set[str]:
        """Load non-empty, non-comment lines from a data file (case-folded)."""
        path = self._data_dir / filename
        if not path.exists():
            return set()
        lines: set[str] = set()
        with open(path) as fh:
            for line in fh:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    lines.add(line)
        return lines

    def _load_allowlist(self) -> None:
        for entry in self._load_file_lines("email_allowlist.txt"):
            if "@" in entry:
                self._allowlist_addresses.add(entry)
            else:
                self._allowlist_domains.add(entry)
        logger.debug(
            "Email allowlist: %d addresses, %d domains",
            len(self._allowlist_addresses),
            len(self._allowlist_domains),
        )

    def _load_blocklist(self) -> None:
        for entry in self._load_file_lines("email_blocklist.txt"):
            if "@" in entry:
                self._blocklist_addresses.add(entry)
            else:
                self._blocklist_domains.add(entry)
        logger.debug(
            "Email blocklist: %d addresses, %d domains",
            len(self._blocklist_addresses),
            len(self._blocklist_domains),
        )

    def _load_disposable_domains(self) -> None:
        self._disposable_domains = self._load_file_lines("disposable_domains.txt")
        logger.debug("Disposable domain list: %d domains", len(self._disposable_domains))

    # ── Internal checks ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_domain(email: str) -> str:
        return email.lower().split("@")[-1]

    def _domain_chain(self, domain: str) -> list[str]:
        """Return domain and all parent domains for hierarchical matching."""
        parts = domain.split(".")
        return [".".join(parts[i:]) for i in range(len(parts) - 1)]

    def _is_allowlisted(self, email: str) -> bool:
        if email in self._allowlist_addresses:
            return True
        domain = self._extract_domain(email)
        return any(d in self._allowlist_domains for d in self._domain_chain(domain))

    def _is_blocklisted(self, email: str) -> tuple[bool, str]:
        if email in self._blocklist_addresses:
            return True, f"Address '{email}' is in your blocklist"
        domain = self._extract_domain(email)
        for d in self._domain_chain(domain):
            if d in self._blocklist_domains:
                return True, f"Domain '{d}' is in your blocklist"
        return False, ""

    def _is_dangerous(self, email: str) -> tuple[bool, str]:
        domain = self._extract_domain(email)
        if domain in DANGEROUS_EXACT_DOMAINS:
            return True, f"Domain '{domain}' is a known dangerous/abuse domain"
        for pattern in DANGEROUS_DOMAIN_PATTERNS:
            if pattern.search(domain):
                return True, f"Domain '{domain}' matches dangerous pattern"
        return False, ""

    def _is_disposable(self, email: str) -> tuple[bool, str]:
        domain = self._extract_domain(email)
        for d in self._domain_chain(domain):
            if d in self._disposable_domains:
                return True, f"Domain '{d}' is a known disposable/temporary email service"
        return False, ""

    def _is_suspicious(self, email: str) -> tuple[bool, str]:
        for pattern, reason in SUSPICIOUS_PATTERNS:
            if pattern.search(email):
                return True, reason
        return False, ""

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, email: str) -> EmailCheckResult:
        """
        Validate a single recipient address through all five layers.

        Returns an :class:`EmailCheckResult` with verdict, reason, and address.
        """
        email = email.strip().lower()

        # Basic format sanity check
        if "@" not in email or "." not in email.split("@")[-1]:
            return EmailCheckResult(
                verdict=EmailVerdict.DENY_DANGEROUS,
                reason=f"Invalid email format: '{email}'",
                address=email,
            )

        # Layer 1: Allowlist overrides everything
        if self._is_allowlisted(email):
            return EmailCheckResult(
                verdict=EmailVerdict.ALLOW,
                reason="Address/domain is in your allowlist",
                address=email,
            )

        # Layer 2: User blocklist
        blocked, reason = self._is_blocklisted(email)
        if blocked:
            return EmailCheckResult(verdict=EmailVerdict.DENY_BLOCKLIST, reason=reason, address=email)

        # Layer 3: Hardcoded dangerous domains
        dangerous, reason = self._is_dangerous(email)
        if dangerous:
            return EmailCheckResult(verdict=EmailVerdict.DENY_DANGEROUS, reason=reason, address=email)

        # Layer 4: Disposable email domains (configurable)
        if self._block_disposable:
            disposable, reason = self._is_disposable(email)
            if disposable:
                return EmailCheckResult(verdict=EmailVerdict.DENY_DISPOSABLE, reason=reason, address=email)

        # Layer 5: Suspicious heuristics (warn only, never hard-deny)
        suspicious, reason = self._is_suspicious(email)
        if suspicious:
            return EmailCheckResult(verdict=EmailVerdict.WARN, reason=reason, address=email)

        return EmailCheckResult(verdict=EmailVerdict.ALLOW, reason="No issues found", address=email)

    def check_all(self, recipients: list[str]) -> list[EmailCheckResult]:
        """Check every address in *recipients*. Returns one result per address."""
        return [self.check(r) for r in recipients if r.strip()]
