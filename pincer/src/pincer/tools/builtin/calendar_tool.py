"""
Google Calendar tools — OAuth2 integration.

Provides calendar_today(), calendar_week(), calendar_create().
Authentication via InstalledAppFlow; tokens stored at data/google_token.json.

Tools are registered in cli.py via tools.register().
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pincer.config import get_settings

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
    token_path = settings.data_dir / "google_token.json"
    credentials_path = settings.data_dir / "google_credentials.json"

    if not credentials_path.exists():
        raise FileNotFoundError(
            "SETUP REQUIRED: Google OAuth client credentials not found at "
            f"{credentials_path}. Download the JSON from Google Cloud Console "
            "-> APIs & Services -> Credentials -> OAuth 2.0 Client IDs, "
            "then save it as data/google_credentials.json."
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
                "Delete data/google_token.json and re-authorize with: "
                "pincer auth-google"
            ) from e

    raise FileNotFoundError(
        "Google token exists but is invalid (no refresh token). "
        "Delete data/google_token.json and re-authorize with: "
        "pincer auth-google"
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
        now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
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

async def calendar_create(
    title: str,
    start_time: str,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Create a new Google Calendar event. Returns confirmation string."""
    try:
        service = _get_service()
        settings = get_settings()
        start_dt = datetime.fromisoformat(start_time)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        tz = str(start_dt.tzinfo) if start_dt.tzinfo else settings.timezone

        event_body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz},
        }
        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location

        created = (
            service.events()
            .insert(calendarId=calendar_id, body=event_body)
            .execute()
        )

        link = created.get("htmlLink", "")
        logger.info("Calendar event created: %s at %s", title, start_dt.isoformat())
        return (
            f"Event created: '{title}' on {start_dt.strftime('%B %d at %H:%M')}\n"
            f"Link: {link}"
        )

    except FileNotFoundError as e:
        return str(e)
    except ValueError as e:
        return f"Invalid date format: {e}"
    except Exception as e:
        logger.error("calendar_create error: %s", e, exc_info=True)
        return f"Error creating event: {e}"
