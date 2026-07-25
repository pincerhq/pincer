---
name: Scheduler and proactive briefings
description: How Pincer's cron scheduler, morning briefings, and event triggers work, and how to configure or customize them. Load this when the user asks to schedule a recurring task, change their briefing time or sections, or set up a reactive notification.
---

# Scheduler and proactive briefings

Pincer has two proactive systems, both driven by a persistent SQLite-backed
scheduler:

## Morning briefings

A daily briefing is generated per user with up to four sections: weather
(OpenWeatherMap), calendar (reuses the calendar tool), email (reuses the
email tool), and news (NewsAPI), plus any custom sections the user has
configured. Settings live in a `briefing_config` table per
`pincer_user_id`:

- `sections` — which of weather/calendar/email/news are included
- `custom_sections` — user-defined additions
- `weather_location` — e.g. `"Berlin,DE"`
- `news_topics` — e.g. `["technology", "business"]`

The briefing time and timezone are global config (`briefing_time`,
default `07:00`, and `briefing_timezone`), not per-user.

If the user wants to change what's in their briefing, update their
`briefing_config` row rather than the global schedule.

## Cron scheduler

General-purpose scheduling uses standard cron expressions, persisted so
schedules survive restarts, checked every 60 seconds, timezone-aware per
schedule. Action handlers are registered by type (`briefing`, `custom`,
etc.) — when a user asks "remind me every Monday at 9am to...", this is
the mechanism to use: create a schedule with a cron expression and an
action, not an ad-hoc sleep/loop.

## Event triggers (reactive, not scheduled)

Separate from cron, event triggers fire on external events rather than on
a schedule: new email arrival, calendar reminders (15 minutes before an
event), and custom webhooks. Deduplication prevents the same event from
notifying twice. Use these when the user wants "notify me when X happens"
rather than "do X at time Y".
