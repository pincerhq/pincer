"""
Pilot onboarding preflight (Sprint 10, T10.1).

Target: a new customer live in ≤ 2 hours. That only holds if the steps are
written down, timed, and shrinking — so the step list here is **data**, not
prose. Every step declares whether it is automated, how long it takes, and (when
manual) whether it *could* be automated.

That last field is the point. "Every manual step logged as an automation
candidate" is a good intention until the list lives in someone's head; here
`automation_candidates()` ranks what to automate next by minutes saved, so
T10.3's "automate the top 3 manual steps" is a measurable change rather than a
judgement call.

`preflight()` checks the automatable half against the live configuration and
tells the operator exactly what is still missing, so nobody discovers a missing
Twilio number after the customer is on the phone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.config import Settings

logger = logging.getLogger(__name__)


class StepStatus(StrEnum):
    READY = "ready"
    MISSING = "missing"
    MANUAL = "manual"  # cannot be checked from here; a human confirms it
    SKIPPED = "skipped"


@dataclass
class OnboardingStep:
    key: str
    title: str
    minutes: int
    automated: bool
    detail: str = ""
    #: False for steps that inherently need a human (signing a contract, the
    #: handover conversation). Kept as a field rather than inferred from the
    #: note, so `automation_candidates()` ranks work that can actually be done.
    automatable: bool = True
    #: Why this is still manual, and what automating it would take.
    automation_note: str = ""
    #: Returns (status, message). None means "no automated check exists".
    check: Callable[[Settings], tuple[StepStatus, str]] | None = field(default=None, repr=False)


@dataclass
class StepResult:
    step: OnboardingStep
    status: StepStatus
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.step.key,
            "title": self.step.title,
            "minutes": self.step.minutes,
            "automated": self.step.automated,
            "status": str(self.status),
            "message": self.message,
            "automatable": self.step.automatable,
            "automation_note": self.step.automation_note,
        }


# ── Individual checks ────────────────────────────────────────────────


def _secret(settings: Settings | Any, name: str) -> str:
    """Read a SecretStr field, tolerating plain-string test doubles."""
    value: Any = getattr(settings, name, "")
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "")
    return str(value or "")


def _check_twilio(settings: Settings | Any) -> tuple[StepStatus, str]:
    account = str(getattr(settings, "twilio_account_sid", "") or "")
    token = _secret(settings, "twilio_auth_token")
    number = str(getattr(settings, "twilio_phone_number", "") or "")
    missing = [
        name
        for name, value in (
            ("PINCER_TWILIO_ACCOUNT_SID", account),
            ("PINCER_TWILIO_AUTH_TOKEN", token),
            ("PINCER_TWILIO_PHONE_NUMBER", number),
        )
        if not value
    ]
    if missing:
        return StepStatus.MISSING, "missing: " + ", ".join(missing)
    if not number.startswith("+49"):
        return StepStatus.READY, f"{number} configured (note: not a +49 number — intended for a DACH pilot?)"
    return StepStatus.READY, f"{number} configured"


def _check_webhook(settings: Settings | Any) -> tuple[StepStatus, str]:
    url = str(getattr(settings, "voice_webhook_base_url", "") or "").strip()
    if not url.startswith("https://"):
        return StepStatus.MISSING, f"PINCER_VOICE_WEBHOOK_BASE_URL must be a public HTTPS URL (got {url or 'empty'})"
    for marker in ("ngrok", "trycloudflare", "localhost", "127.0.0.1", "loca.lt"):
        if marker in url:
            return StepStatus.MISSING, f"{url} is a tunnel/localhost — a pilot needs a real hostname"
    return StepStatus.READY, url


def _check_calendar(settings: Settings | Any) -> tuple[StepStatus, str]:
    """Google Calendar OAuth — appointment scheduling is dead without it."""
    from pathlib import Path

    token_paths = [
        Path.home() / ".pincer" / "google_token.json",
        Path(str(getattr(settings, "data_dir", "data"))) / "google_token.json",
    ]
    for path in token_paths:
        if path.exists():
            return StepStatus.READY, f"OAuth token present ({path})"
    return StepStatus.MISSING, "no Google OAuth token found — run `pincer setup-google`"


def _check_voice(settings: Settings | Any) -> tuple[StepStatus, str]:
    key = _secret(settings, "elevenlabs_api_key")
    if not key:
        return StepStatus.MISSING, "PINCER_ELEVENLABS_API_KEY not set (calls fall back to Twilio's default voice)"
    ids = {lang: str(getattr(settings, f"elevenlabs_voice_id_{lang}", "") or "") for lang in ("en", "de", "uk")}
    fallback = str(getattr(settings, "elevenlabs_voice_id", "") or "")
    configured = {lang: vid for lang, vid in ids.items() if vid}
    if not configured and not fallback:
        return StepStatus.MISSING, "no voice ID configured — `pincer voice list` to find one"
    default_lang = str(getattr(settings, "voice_default_language", "en") or "en")
    if default_lang not in configured and not fallback:
        return StepStatus.MISSING, f"no voice configured for the default language ({default_lang})"
    return StepStatus.READY, f"voices: {configured or {'fallback': fallback}}"


def _check_compliance(settings: Settings | Any) -> tuple[StepStatus, str]:
    problems: list[str] = []
    if str(getattr(settings, "voice_consent_mode", "")) != "two_party":
        problems.append("consent_mode must be two_party for DE/CH")
    if int(getattr(settings, "voice_transcript_retention_days", 0) or 0) <= 0:
        problems.append("transcript retention disabled")
    tz = str(getattr(settings, "voice_timezone", "") or getattr(settings, "timezone", "") or "")
    if not tz.startswith("Europe/"):
        problems.append(f"timezone {tz or 'unset'} is not Europe/*")
    if problems:
        return StepStatus.MISSING, "; ".join(problems)
    return StepStatus.READY, "two-party consent, retention, Europe/* timezone"


def _check_api_tokens(settings: Settings | Any) -> tuple[StepStatus, str]:
    dashboard = _secret(settings, "dashboard_token")
    web_chat = _secret(settings, "web_chat_token")
    if not dashboard or not web_chat:
        return StepStatus.MISSING, "both PINCER_DASHBOARD_TOKEN and PINCER_WEB_CHAT_TOKEN are required"
    if dashboard == web_chat:
        return StepStatus.MISSING, "the two tokens must differ"
    if min(len(dashboard), len(web_chat)) < 16:
        return StepStatus.MISSING, "token too short — use 32+ chars"
    return StepStatus.READY, "dashboard + web chat tokens set and distinct"


def _check_ops(settings: Settings | Any) -> tuple[StepStatus, str]:
    recipient = str(getattr(settings, "ops_user_id", "") or getattr(settings, "default_user_id", "") or "")
    email = str(getattr(settings, "ops_alert_email", "") or "")
    if not recipient and not email:
        return StepStatus.MISSING, "no alert recipient — set PINCER_OPS_USER_ID or PINCER_OPS_ALERT_EMAIL"
    canary = bool(getattr(settings, "voice_canary_enabled", False)) and bool(
        str(getattr(settings, "voice_canary_number", "") or "").strip()
    )
    return (
        StepStatus.READY,
        f"alerts → {recipient or email}" + ("; canary configured" if canary else "; canary NOT configured"),
    )


def _check_abuse_limits(settings: Settings | Any) -> tuple[StepStatus, str]:
    from pincer.voice.safety_gates import parse_quiet_hours

    gaps: list[str] = []
    if int(getattr(settings, "voice_daily_call_limit", 0) or 0) <= 0:
        gaps.append("no global daily cap")
    if parse_quiet_hours(str(getattr(settings, "voice_quiet_hours", "") or "")) is None:
        gaps.append("no quiet hours (§7 UWG exposure)")
    if gaps:
        return StepStatus.MISSING, "; ".join(gaps)
    return StepStatus.READY, "daily cap, target cooldown, quiet hours active"


# ── The step list ────────────────────────────────────────────────────
#
# Minutes are wall-clock for an operator who has done it before. They are the
# baseline the ≤2h target is measured against — update them when reality
# disagrees, and the automation ranking updates with them.

STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        key="contract",
        title="AVV/DPA signed with the customer",
        minutes=30,
        automated=False,
        detail="docs/guides/avv-annex.md, reviewed by counsel first",
        automatable=False,
        automation_note="Not automatable — it is a signature. Can be shortened by having counsel pre-approve "
        "the annex once, so each pilot is a countersign rather than a review.",
    ),
    OnboardingStep(
        key="host",
        title="Production host provisioned and deployed",
        minutes=20,
        automated=True,
        detail="scripts/deploy.sh with .env.production",
        automation_note="",
    ),
    OnboardingStep(
        key="twilio",
        title="Twilio account, +49 number, regulatory bundle",
        minutes=25,
        automated=False,
        detail="Number purchase + DACH regulatory bundle approval",
        automation_note="HIGH VALUE: number purchase and webhook wiring are Twilio API calls. The regulatory "
        "bundle is the human part and can take days — start it before the onboarding session, not during.",
        check=_check_twilio,
    ),
    OnboardingStep(
        key="webhook",
        title="Public HTTPS hostname + Twilio webhook URLs configured",
        minutes=10,
        automated=False,
        detail="DNS A record, then point the number at /api/apps/twilio/webhook and /fallback",
        automation_note="HIGH VALUE: pointing the number at our webhooks is two Twilio API calls and is done "
        "identically every time.",
        check=_check_webhook,
    ),
    OnboardingStep(
        key="api_tokens",
        title="Dashboard and web-chat tokens generated",
        minutes=2,
        automated=True,
        detail="Two distinct 32+ char tokens in .env.production",
        automation_note="",
        check=_check_api_tokens,
    ),
    OnboardingStep(
        key="calendar",
        title="Google Calendar OAuth completed",
        minutes=10,
        automated=False,
        detail="`pincer setup-google`, customer signs in with their business account",
        automation_note="Cannot be fully automated (the customer must consent), but the consent URL can be "
        "emailed ahead of the session instead of walked through live.",
        check=_check_calendar,
    ),
    OnboardingStep(
        key="voice",
        title="ElevenLabs voice selected or cloned",
        minutes=20,
        automated=False,
        detail="docs/guides/custom-voices.md — includes the speaker-consent requirement",
        automation_note="Cloning needs a recording and the speaker's consent. Offering a curated set of stock "
        "German voices as the default removes this step entirely for customers who do not need a clone.",
        check=_check_voice,
    ),
    OnboardingStep(
        key="compliance",
        title="DACH compliance settings applied",
        minutes=3,
        automated=True,
        detail="two-party consent, 90-day retention, Europe/Berlin",
        automation_note="",
        check=_check_compliance,
    ),
    OnboardingStep(
        key="abuse_limits",
        title="Outbound abuse limits reviewed with the customer",
        minutes=5,
        automated=True,
        detail="daily cap, target cooldown, quiet hours — confirm the defaults suit their business hours",
        automation_note="",
        check=_check_abuse_limits,
    ),
    OnboardingStep(
        key="ops",
        title="Ops alerting and canary wired",
        minutes=5,
        automated=True,
        detail="PINCER_OPS_USER_ID + canary target number",
        automation_note="",
        check=_check_ops,
    ),
    OnboardingStep(
        key="channel",
        title="Customer's Telegram / web access set up",
        minutes=10,
        automated=False,
        detail="Bot token, allowlist the customer's user ID, or hand over the web chat URL + token",
        automation_note="MEDIUM VALUE: allowlisting is a config write; the customer finding their Telegram user "
        "ID is the slow part and could be replaced by a one-time pairing link.",
    ),
    OnboardingStep(
        key="verify",
        title="Verification: doctor, canary, one supervised live call",
        minutes=15,
        automated=True,
        detail="`pincer doctor --production`, `pincer voice ops canary`, then one real call with the customer",
        automation_note="",
    ),
    OnboardingStep(
        key="handover",
        title="Quickstart walkthrough + expectations doc",
        minutes=15,
        automated=False,
        detail="docs/guides/pilot-quickstart-de.md / -en.md, docs/operations/pilot-expectations.md",
        automatable=False,
        automation_note="Not automatable, and should not be — this is the conversation where the customer learns "
        "what the system will and will not do.",
    ),
)

TARGET_MINUTES = 120


def total_minutes() -> int:
    return sum(step.minutes for step in STEPS)


def manual_minutes() -> int:
    return sum(step.minutes for step in STEPS if not step.automated)


def automatable_minutes() -> int:
    """Minutes currently spent by hand that a script could take over."""
    return sum(step.minutes for step in automation_candidates())


def automation_candidates() -> list[OnboardingStep]:
    """Manual steps with an automation note, ranked by minutes saved.

    T10.3 says "automate the top 3 manual steps" — this is that list, ordered
    by what it actually costs to keep doing by hand.
    """
    return sorted(
        (s for s in STEPS if not s.automated and s.automatable and s.automation_note),
        key=lambda s: s.minutes,
        reverse=True,
    )


# ── Preflight ────────────────────────────────────────────────────────


def preflight(settings: Settings | Any) -> list[StepResult]:
    """Check every automatable step against the live configuration."""
    results: list[StepResult] = []
    for step in STEPS:
        if step.check is None:
            results.append(
                StepResult(step=step, status=StepStatus.MANUAL, message="confirm manually — no automated check")
            )
            continue
        try:
            status, message = step.check(settings)
        except Exception as e:  # pragma: no cover — a broken check must not block onboarding
            logger.exception("Onboarding check %s failed", step.key)
            status, message = StepStatus.MISSING, f"check failed: {e}"
        results.append(StepResult(step=step, status=status, message=message))
    return results


def blocking(results: list[StepResult]) -> list[StepResult]:
    return [r for r in results if r.status is StepStatus.MISSING]


def render_checklist(customer: str, results: list[StepResult]) -> str:
    """Per-customer onboarding checklist, ready to paste into the pilot notes."""
    icons = {
        StepStatus.READY: "✅",
        StepStatus.MISSING: "❌",
        StepStatus.MANUAL: "☐",
        StepStatus.SKIPPED: "–",
    }
    lines = [
        f"# Onboarding — {customer}",
        "",
        f"Target: ≤ {TARGET_MINUTES} min. Estimated from the step list: **{total_minutes()} min** "
        f"({manual_minutes()} min manual).",
        "",
        "| | Step | Min | Status |",
        "|---|---|---|---|",
    ]
    for result in results:
        lines.append(f"| {icons[result.status]} | {result.step.title} | {result.step.minutes} | {result.message} |")

    missing = blocking(results)
    if missing:
        lines.extend(["", "## Blocking", ""])
        for result in missing:
            lines.append(f"- **{result.step.title}** — {result.message}")
            if result.step.detail:
                lines.append(f"  - {result.step.detail}")

    lines.extend(
        [
            "",
            "## Record the actual time",
            "",
            "| Step | Estimated | Actual | Notes |",
            "|---|---|---|---|",
        ]
    )
    for step in STEPS:
        lines.append(f"| {step.title} | {step.minutes} | | |")
    lines.extend(
        [
            "",
            "Anything that took materially longer than its estimate is either a wrong estimate or a new "
            "automation candidate. Update `pincer/onboarding.py` rather than remembering it.",
            "",
        ]
    )
    return "\n".join(lines)
