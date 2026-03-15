"""
Google Calendar tools — OAuth2 integration.

Provides calendar_today(), calendar_week(), calendar_create().
Authentication via InstalledAppFlow; tokens stored at data/google_token.json.

Tools are registered in cli.py via tools.register().

calendar_create behavior:
- Strict response validation: success only when API returns both id and htmlLink
- IANA timezone: naive datetimes use settings.timezone; fixed offsets fall back to IANA
- Success message includes htmlLink and calendar_id (when non-primary) for verification
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pincer.config import get_settings
from zoneinfo import ZoneInfo

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_credentials():  # type: ignore[no-untyped-def]
    """Get or refresh Google OAuth2 credentials.

    Never attempts the interactive browser consent flow — that belongs
    in the ``pincer auth-google`` CLI command.  This function only loads
    an existing token and refreshes it if possible.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    oauth_dir = settings.google_oauth_dir()
    token_path = oauth_dir / "google_token.json"
    credentials_path = oauth_dir / "google_credentials.json"

    if not credentials_path.exists():
        raise FileNotFoundError(
            "SETUP REQUIRED: Google OAuth client credentials not found at "
            f"{credentials_path}. Download the JSON from Google Cloud Console "
            "-> APIs & Services -> Credentials -> OAuth 2.0 Client IDs, "
            f"then save it as {credentials_path} or in ~/.pincer/"
        )

    if not token_path.exists():
        raise FileNotFoundError(
            "SETUP REQUIRED: No Google token found. Run the one-time OAuth "
            "consent flow first:  pincer auth-google"
        )

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            logger.info("Google OAuth token refreshed successfully")
            return creds
        except Exception as e:
            raise FileNotFoundError(
                f"Google token refresh failed: {e}. "
                f"Delete {token_path} and re-authorize with: pincer auth-google"
            ) from e

    raise FileNotFoundError(
        "Google token exists but is invalid (no refresh token). "
        f"Delete {token_path} and re-authorize with: pincer auth-google"
    )


def _get_service():  # type: ignore[no-untyped-def]
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_get_credentials())


def _format_event(event: dict[str, Any]) -> str:
    """Format a single calendar event into a readable string."""
    start = event.get("start", {})
    end = event.get("end", {})
    start_str = start.get("dateTime", start.get("date", ""))
    is_all_day = "date" in start and "dateTime" not in start

    if is_all_day:
        time_display = "All day"
    else:
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end.get("dateTime", ""))
            time_display = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
        except (ValueError, TypeError):
            time_display = start_str

    title = event.get("summary", "(No title)")
    location = event.get("location", "")
    loc_str = f" | {location}" if location else ""
    return f"  {time_display} — {title}{loc_str}"


# ── Tool: calendar_today ─────────────────────────

async def calendar_today(calendar_id: str = "primary") -> str:
    """Get today's calendar events. Returns formatted string."""
    try:
        service = _get_service()
        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )

        events = result.get("items", [])
        if not events:
            return f"Calendar is clear today ({now.strftime('%A, %B %d')})."

        lines = [f"Today's schedule ({now.strftime('%A, %B %d')}) — {len(events)} event(s):\n"]
        for e in events:
            lines.append(_format_event(e))
        return "\n".join(lines)

    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        logger.error("calendar_today error: %s", e, exc_info=True)
        return f"Error reading calendar: {e}"


# ── Tool: calendar_week ──────────────────────────

async def calendar_week(calendar_id: str = "primary") -> str:
    """Get this week's calendar events. Returns formatted string."""
    try:
        service = _get_service()
        now = datetime.now(UTC)
        end = now + timedelta(days=7)

        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
            .execute()
        )

        events = result.get("items", [])
        if not events:
            return f"No events in the next 7 days ({now.strftime('%b %d')} - {end.strftime('%b %d')})."

        days: dict[str, list[str]] = {}
        for e in events:
            start = e.get("start", {})
            date_str = start.get("dateTime", start.get("date", ""))[:10]
            try:
                day_label = datetime.fromisoformat(date_str).strftime("%A, %B %d")
            except (ValueError, TypeError):
                day_label = date_str
            days.setdefault(day_label, []).append(_format_event(e))

        lines = [
            f"Week ahead ({now.strftime('%b %d')} - {end.strftime('%b %d')}) "
            f"— {len(events)} event(s):\n"
        ]
        for day, day_events in days.items():
            lines.append(f"\n{day}:")
            lines.extend(day_events)
        return "\n".join(lines)

    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        logger.error("calendar_week error: %s", e, exc_info=True)
        return f"Error reading calendar: {e}"


# ── Tool: calendar_create ────────────────────────

def _resolve_timezone(start_dt: datetime, settings_tz: str) -> str:
    """Resolve IANA timezone for Google Calendar API.

    Google expects IANA names (e.g. Europe/Berlin). Fixed offsets (UTC+01:00)
    are not ideal, so we fall back to settings.timezone for those.
    """
    tzinfo = start_dt.tzinfo
    if tzinfo is None:
        return settings_tz
    if hasattr(tzinfo, "key"):  # ZoneInfo
        return tzinfo.key
    # Fixed offset (e.g. datetime.timezone) — use IANA from settings
    return settings_tz


async def calendar_create(
    title: str,
    start_time: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Create a new Google Calendar event. Returns confirmation string.

    execute() is blocking; the tool waits for the full API response before
    returning, so there is no fire-and-forget behavior.
    """
    try:
        service = _get_service()
        settings = get_settings()
        start_dt = datetime.fromisoformat(start_time)

        # Naive datetime: interpret in user's timezone
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=ZoneInfo(settings.timezone))

        end_dt = start_dt + timedelta(minutes=duration_minutes)
        tz = _resolve_timezone(start_dt, settings.timezone)

        event_body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        }
        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location

        logger.debug(
            "calendar_create event_body: start=%s end=%s timeZone=%s",
            event_body["start"],
            event_body["end"],
            tz,
        )

        # execute() blocks until full response; no fire-and-forget
        created = (
            service.events()
            .insert(calendarId=calendar_id, body=event_body)
            .execute()
        )

        event_id = created.get("id", "")
        link = created.get("htmlLink", "")
        if not event_id or not link:
            return (
                "Error: Calendar API did not return event ID or link. "
                "Creation may have failed."
            )

        logger.info("Calendar event created: %s at %s", title, start_dt.isoformat())

        lines = [
            f"Event created: '{title}' on {start_dt.strftime('%B %d at %H:%M')}",
            f"Link: {link}",
        ]
        if calendar_id != "primary":
            lines.append(f"Calendar: {calendar_id}")
        return "\n".join(lines)

    except FileNotFoundError as e:
        return str(e)
    except ValueError as e:
        return f"Invalid date format: {e}"
    except Exception as e:
        logger.error("calendar_create error: %s", e, exc_info=True)
        return f"Error creating event: {e}"
