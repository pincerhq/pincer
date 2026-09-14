"""
Business profile (Sprint 12, §4) — the ONLY knowledge source the inbound
receptionist answers from.

Loaded once at startup from ``PINCER_BUSINESS_PROFILE`` (YAML) into a
validated :class:`BusinessProfile`; with ``PINCER_RECEPTIONIST_ENABLED=true``
a missing or invalid profile is a startup :class:`ProfileError` (a
``ConfigError``) whose message names the exact field. No hot reload in v1 —
restart to apply.

The loaded profile is held in a module-level slot (``set_profile`` /
``get_profile``) so the webhook, the channel, the tool, and the doctor read
the same object; tests install a profile directly.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from pincer.exceptions import ConfigError

logger = logging.getLogger(__name__)

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "de", "uk")
E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
MAX_FAQ = 50
MAX_FAQ_ANSWER_CHARS = 300

DEFAULT_PROFILE_PATH = "./business_profile.yaml"


class ProfileError(ConfigError):
    """The business profile is missing or invalid (names the exact field)."""


def parse_range(value: str) -> tuple[time, time]:
    """'08:00-12:00' → (time(8), time(12)); raises ValueError with the reason."""
    match = RANGE_RE.match(str(value or ""))
    if not match:
        raise ValueError(f"{value!r} is not 'HH:MM-HH:MM'")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError as e:
        raise ValueError(f"{value!r}: {e}") from e
    if start >= end:
        raise ValueError(f"{value!r}: start must be before end")
    return start, end


class BusinessInfo(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    languages: list[str] = Field(min_length=1)
    timezone: str

    @field_validator("languages")
    @classmethod
    def _languages_supported(cls, value: list[str]) -> list[str]:
        normalized = [str(v).strip().lower()[:2] for v in value]
        for lang in normalized:
            if lang not in SUPPORTED_LANGUAGES:
                raise ValueError(f"unsupported language {lang!r} (allowed: {', '.join(SUPPORTED_LANGUAGES)})")
        return normalized

    @field_validator("timezone")
    @classmethod
    def _timezone_valid(cls, value: str) -> str:
        try:
            ZoneInfo(str(value))
        except (ZoneInfoNotFoundError, ValueError, TypeError) as e:
            raise ValueError(f"unknown IANA timezone {value!r}") from e
        return str(value)


class FAQItem(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    a: str = Field(min_length=1, max_length=MAX_FAQ_ANSWER_CHARS)


class BookingConfig(BaseModel):
    enabled: bool = True
    event_duration_min: int = Field(default=30, ge=5, le=480)
    ask_email: bool = False
    event_title_template: str = Field(default="Termin: {caller_name}", min_length=1, max_length=120)


class TransferConfig(BaseModel):
    enabled: bool = False
    target: str = ""
    announce: str = ""

    @model_validator(mode="after")
    def _target_required_when_enabled(self) -> TransferConfig:
        if self.enabled:
            if not self.target:
                raise ValueError("target is required when transfer.enabled is true")
            if not E164_RE.match(self.target):
                raise ValueError(f"target {self.target!r} is not an E.164 number")
        return self


class AfterHoursConfig(BaseModel):
    message: str = ""


class BusinessProfile(BaseModel):
    version: Literal[1]
    business: BusinessInfo
    hours: dict[str, list[str]]
    services: list[str] = Field(default_factory=list)
    faq: list[FAQItem] = Field(default_factory=list, max_length=MAX_FAQ)
    address: str = ""
    booking: BookingConfig = Field(default_factory=BookingConfig)
    transfer: TransferConfig = Field(default_factory=TransferConfig)
    after_hours: AfterHoursConfig = Field(default_factory=AfterHoursConfig)

    @field_validator("hours")
    @classmethod
    def _hours_complete_and_parseable(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = [k for k in value if k not in WEEKDAYS]
        if unknown:
            raise ValueError(f"unknown weekday key(s) {unknown} (use {', '.join(WEEKDAYS)})")
        missing = [d for d in WEEKDAYS if d not in value]
        if missing:
            raise ValueError(f"missing weekday(s) {missing} — use [] for closed days")
        for day in WEEKDAYS:
            ranges = value.get(day) or []
            if not isinstance(ranges, list):
                raise ValueError(f"hours.{day} must be a list of 'HH:MM-HH:MM' ranges")
            parsed: list[tuple[time, time]] = []
            for raw in ranges:
                try:
                    parsed.append(parse_range(str(raw)))
                except ValueError as e:
                    raise ValueError(f"hours.{day}: {e}") from e
            parsed.sort()
            for (_s1, e1), (s2, _e2) in zip(parsed, parsed[1:], strict=False):
                if s2 < e1:
                    raise ValueError(f"hours.{day}: ranges overlap")
        return value

    # ── Helpers ──────────────────────────────────────────

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.business.timezone)

    @property
    def default_language(self) -> str:
        return self.business.languages[0]

    def hours_for(self, weekday: int) -> list[tuple[time, time]]:
        """Opening ranges for a weekday (Monday=0), sorted."""
        key = WEEKDAYS[weekday % 7]
        return sorted(parse_range(r) for r in (self.hours.get(key) or []))

    def is_open(self, at: datetime) -> bool:
        """True when the profile says "open" at the given instant (converted
        to the business timezone — DST-safe because the ranges are wall-clock)."""
        local = at.astimezone(self.tz) if at.tzinfo else at.replace(tzinfo=self.tz)
        now_t = local.time()
        return any(start <= now_t < end for start, end in self.hours_for(local.weekday()))

    def faq_lookup(self, question: str) -> FAQItem | None:
        """Cheap keyword match (the LLM does the semantic match; this is the
        deterministic fallback used by the tool and tests)."""
        words = {w for w in re.findall(r"\w+", question.lower()) if len(w) > 3}
        best: tuple[int, FAQItem | None] = (0, None)
        for item in self.faq:
            overlap = len(words & {w for w in re.findall(r"\w+", item.q.lower()) if len(w) > 3})
            if overlap > best[0]:
                best = (overlap, item)
        return best[1]

    def speakable_hours(self, language: str = "de") -> str:
        """'Montag 8 bis 12 und 14 bis 17 Uhr, Dienstag …' — no digits beyond
        clock hours, no ISO."""
        day_names = {
            "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
            "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            "uk": ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"],
        }
        lang = str(language or "de")[:2]
        names = day_names.get(lang, day_names["en"])
        parts: list[str] = []
        for idx in range(7):
            ranges = self.hours_for(idx)
            if not ranges:
                continue
            spans = []
            for start, end in ranges:
                if lang == "de":
                    spans.append(f"{_hm(start)} bis {_hm(end)} Uhr")
                elif lang == "uk":
                    spans.append(f"з {_hm(start)} до {_hm(end)}")
                else:
                    spans.append(f"{_hm(start)} to {_hm(end)}")
            joiner = {"de": " und ", "uk": " та ", "en": " and "}.get(lang, " and ")
            parts.append(f"{names[idx]} {joiner.join(spans)}")
        if not parts:
            return {"de": "derzeit geschlossen", "uk": "наразі зачинено", "en": "currently closed"}.get(lang, "")
        return ", ".join(parts)


def _hm(t: time) -> str:
    return f"{t.hour}:{t.minute:02d}" if t.minute else str(t.hour)


# ── Loading ──────────────────────────────────────────────────────────


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for err in error.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"{loc or '<root>'}: {msg}")
    return "; ".join(lines)


def parse_business_profile(data: Any) -> BusinessProfile:
    """Validate already-parsed YAML/JSON data; raises ProfileError naming the field."""
    if not isinstance(data, dict):
        raise ProfileError("business profile must be a mapping at the top level")
    try:
        return BusinessProfile.model_validate(data)
    except ValidationError as e:
        raise ProfileError(f"business profile invalid — {_format_validation_error(e)}") from e


def load_business_profile(path: str | Path) -> BusinessProfile:
    """Read + validate the YAML profile. Raises ProfileError on any problem."""
    import yaml

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ProfileError(f"business profile not found: {file_path} (PINCER_BUSINESS_PROFILE)")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"business profile {file_path} is not valid YAML: {e}") from e
    return parse_business_profile(data)


# ── Process-wide slot ────────────────────────────────────────────────

_profile: BusinessProfile | None = None


def set_profile(profile: BusinessProfile | None) -> None:
    global _profile  # noqa: PLW0603
    _profile = profile


def get_profile() -> BusinessProfile | None:
    return _profile


def receptionist_active(settings: Any) -> bool:
    """True when the inbound receptionist should answer: enabled AND a profile is loaded."""
    return bool(getattr(settings, "receptionist_enabled", False)) and _profile is not None


def load_from_settings(settings: Any) -> BusinessProfile | None:
    """Startup hook: with the receptionist enabled, load + install the profile
    (ProfileError propagates — fail fast); disabled → None (nothing loaded)."""
    if not bool(getattr(settings, "receptionist_enabled", False)):
        set_profile(None)
        return None
    profile = load_business_profile(str(getattr(settings, "business_profile", "") or DEFAULT_PROFILE_PATH))
    set_profile(profile)
    logger.info(
        "Receptionist profile loaded: %s (%s, languages=%s, booking=%s, transfer=%s)",
        profile.business.name,
        profile.business.timezone,
        ",".join(profile.business.languages),
        profile.booking.enabled,
        profile.transfer.enabled,
    )
    return profile


__all__ = [
    "DEFAULT_PROFILE_PATH",
    "MAX_FAQ",
    "MAX_FAQ_ANSWER_CHARS",
    "SUPPORTED_LANGUAGES",
    "WEEKDAYS",
    "AfterHoursConfig",
    "BookingConfig",
    "BusinessInfo",
    "BusinessProfile",
    "FAQItem",
    "ProfileError",
    "TransferConfig",
    "get_profile",
    "load_business_profile",
    "load_from_settings",
    "parse_business_profile",
    "parse_range",
    "receptionist_active",
    "set_profile",
]
