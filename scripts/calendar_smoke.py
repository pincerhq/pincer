#!/usr/bin/env python3
"""Live calendar smoke check (calendar reliability, issue #297) — checklist + checker.

Replays the 2026-10-31 reproduction against a real Google account through a
running Pincer. YOU send the messages from your phone; this script prints them
and then reads the real calendar to check what Pincer actually stored. It never
talks to Pincer and never runs in CI (not a test module, not collected).

Every event it reads or deletes carries a unique run tag in its title (e.g.
"Smoke-7F3A2C Alpha"); events without the tag are never touched. It uses the
existing Google token location (PINCER_GOOGLE_TOKEN_JSON or the token file),
the same one `pincer run` uses — run it from the Pincer checkout.

Usage:
    # 1. Print a run tag, the dates and the messages to send (channel A = e.g. WhatsApp,
    #    channel B = e.g. Telegram — two different chats with Pincer)
    uv run python scripts/calendar_smoke.py plan

    # 2. After sending messages 1, 2 and 6a: record the event IDs
    uv run python scripts/calendar_smoke.py snapshot --tag Smoke-7F3A2C --tomorrow 2026-11-02 \\
        --far-day 2026-11-13 --out smoke-7F3A2C.json

    # 3. After sending the rest: print the PASS/FAIL table
    uv run python scripts/calendar_smoke.py check --tag Smoke-7F3A2C --tomorrow 2026-11-02 \\
        --far-day 2026-11-13 --snapshot smoke-7F3A2C.json

    # 4. Clean up: list the tagged events (dry run), then delete them
    uv run python scripts/calendar_smoke.py cleanup --tag Smoke-7F3A2C
    uv run python scripts/calendar_smoke.py cleanup --tag Smoke-7F3A2C --confirm

Exit code of `check`: 0 when every automatic row passes, 1 otherwise. Rows
marked MANUAL (Pincer's reply wording, log lines) are for you to tick.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TAG_PREFIX = "Smoke-"
TAG_RE = re.compile(r"^Smoke-[0-9A-F]{6}$")
CALENDAR_ID = "primary"
FAR_DAYS = 12  # step 2: an event 12 days out (the old builtin only looked 7 days ahead)
SEARCH_BEFORE_DAYS = 2
SEARCH_AFTER_DAYS = 40
EVENT_MINUTES = 60

ALPHA = "Alpha"
BETA = "Beta"
MEETING = "Meeting"
MEETING_RENAMED = "Meeting (updated)"

ALPHA_FIRST = time(15, 0)
ALPHA_MOVED = time(17, 0)
BETA_AT = time(15, 0)
MEETING_AT = time(10, 0)


@dataclass(frozen=True)
class Row:
    step: str
    check: str
    status: str  # PASS | FAIL | MANUAL
    detail: str


# ── Pure helpers (no I/O) ──────────────────────────────────────────────────────


def new_tag() -> str:
    return f"{TAG_PREFIX}{secrets.token_hex(3).upper()}"


def title(tag: str, name: str) -> str:
    return f"{tag} {name}"


def messages(tag: str, tomorrow: date, far_day: date) -> list[tuple[str, str, str]]:
    """(step, channel, text) for every message the owner sends."""
    alpha, beta = title(tag, ALPHA), title(tag, BETA)
    meeting, renamed = title(tag, MEETING), title(tag, MEETING_RENAMED)
    return [
        ("1", "A", f'Create an event "{alpha}" tomorrow ({tomorrow.isoformat()}) at 15:00 for one hour.'),
        ("2", "A", f'Create an event "{beta}" on {far_day.isoformat()} at 15:00 for one hour.'),
        ("3", "A", f"What's on my calendar on {far_day.isoformat()}?"),
        ("4", "A", f'Move "{alpha}" to 17:00.'),
        ("5", "A", "What's on my calendar tomorrow?"),
        ("6a", "A", f'Create an event "{meeting}" tomorrow ({tomorrow.isoformat()}) at 10:00 for one hour.'),
        ("6b", "B", f'Rename "{meeting}" to "{renamed}".'),
    ]


def is_tagged(event: dict[str, Any], tag: str) -> bool:
    """Only events whose title starts with this run's tag are ours."""
    return str(event.get("summary") or "").startswith(f"{tag} ")


def _start(event: dict[str, Any]) -> datetime | None:
    raw = (event.get("start") or {}).get("dateTime")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo else None


def _named(events: list[dict[str, Any]], tag: str, name: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("summary") == title(tag, name) and e.get("status") != "cancelled"]


def _wall_clock(event: dict[str, Any], zone: ZoneInfo) -> str:
    start = _start(event)
    if start is None:
        return "(no start)"
    end_raw = (event.get("end") or {}).get("dateTime")
    end = datetime.fromisoformat(str(end_raw)).astimezone(zone) if end_raw else None
    span = f"{start.astimezone(zone):%Y-%m-%d %H:%M}"
    return f"{span}–{end:%H:%M}" if end else span


def _time_row(step: str, what: str, found: list[dict[str, Any]], zone: ZoneInfo, day: date, at: time) -> Row:
    want = datetime.combine(day, at, zone)
    want_text = f"{want:%Y-%m-%d %H:%M}–{want + timedelta(minutes=EVENT_MINUTES):%H:%M} {zone.key}"
    if len(found) != 1:
        return Row(step, f"{what} stored at {want_text}", "FAIL", f"{len(found)} matching event(s)")
    start = _start(found[0])
    ok = start is not None and start == want
    return Row(step, f"{what} stored at {want_text}", "PASS" if ok else "FAIL", _wall_clock(found[0], zone))


def evaluate(
    events: list[dict[str, Any]],
    zone: ZoneInfo,
    tag: str,
    tomorrow: date,
    far_day: date,
    snapshot: dict[str, str] | None = None,
) -> list[Row]:
    """The PASS/FAIL table for one run (pure: ``events`` are what Google returned)."""
    ours = [e for e in events if is_tagged(e, tag)]
    alpha = _named(ours, tag, ALPHA)
    beta = _named(ours, tag, BETA)
    meeting = _named(ours, tag, MEETING)
    renamed = _named(ours, tag, MEETING_RENAMED)
    snap = snapshot or {}
    rows = [
        _time_row("1+4", f'"{title(tag, ALPHA)}" (moved)', alpha, zone, tomorrow, ALPHA_MOVED),
        Row(
            "4",
            "exactly one Alpha after the move",
            "PASS" if len(alpha) == 1 else "FAIL",
            f"{len(alpha)} event(s)",
        ),
        _id_row("4", "Alpha keeps its ID through the move", alpha, snap.get(ALPHA)),
        _time_row("2", f'"{title(tag, BETA)}" ({FAR_DAYS} days out)', beta, zone, far_day, BETA_AT),
        Row(
            "6",
            "cross-channel rename leaves exactly one Meeting, renamed",
            "PASS" if len(renamed) == 1 and not meeting else "FAIL",
            f'{len(meeting)} × "{MEETING}", {len(renamed)} × "{MEETING_RENAMED}"',
        ),
        _id_row("6", "renamed Meeting keeps its original ID", renamed, snap.get(MEETING)),
        _time_row("6", f'"{title(tag, MEETING_RENAMED)}" time unchanged', renamed, zone, tomorrow, MEETING_AT),
        Row(
            "all",
            "no other tagged events (no duplicates)",
            "PASS" if len(ours) == 3 else "FAIL",
            f"{len(ours)} tagged event(s): " + ", ".join(sorted(str(e.get("summary")) for e in ours)),
        ),
        Row("1-6", "Pincer's replies quote the stored times and name the zone", "MANUAL", f"expect {zone.key}"),
        Row("1-6", "log: turn= and call= on every Tool call / Tool result line", "MANUAL", "grep 'Tool result'"),
        Row("1-6", "log: one calendar_write line per write", "MANUAL", "expect 5: 3 creates, the move, the rename"),
    ]
    return rows


def _id_row(step: str, what: str, found: list[dict[str, Any]], snapped: str | None) -> Row:
    if snapped is None:
        return Row(step, what, "MANUAL", "no snapshot given (run `snapshot` after steps 1, 2, 6a)")
    if len(found) != 1:
        return Row(step, what, "FAIL", f"{len(found)} matching event(s); snapshot ID {snapped}")
    got = str(found[0].get("id"))
    return Row(step, what, "PASS" if got == snapped else "FAIL", f"now {got}, snapshot {snapped}")


def snapshot_ids(events: list[dict[str, Any]], tag: str) -> dict[str, str]:
    """IDs of Alpha and Meeting as created (before the move and the rename)."""
    ours = [e for e in events if is_tagged(e, tag)]
    found: dict[str, str] = {}
    for name in (ALPHA, MEETING):
        named = _named(ours, tag, name)
        if len(named) == 1:
            found[name] = str(named[0]["id"])
    return found


def render_table(rows: list[Row]) -> str:
    widths = [max(len(getattr(r, f)) for r in rows) for f in ("step", "check", "status")]
    header = f"{'step':<{widths[0]}}  {'check':<{widths[1]}}  {'result':<{widths[2]}}  detail"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r.step:<{widths[0]}}  {r.check:<{widths[1]}}  {r.status:<{widths[2]}}  {r.detail}")
    automatic = [r for r in rows if r.status != "MANUAL"]
    passed = sum(r.status == "PASS" for r in automatic)
    lines.append(f"\n{passed}/{len(automatic)} automatic checks passed; {len(rows) - len(automatic)} manual.")
    return "\n".join(lines)


# ── Google Calendar I/O ────────────────────────────────────────────────────────


async def _service() -> Any:
    from pincer.integrations.google import get_google_factory

    factory = get_google_factory()
    if factory is None or "calendar" not in factory.auth.services:
        sys.exit("No Google token with the calendar scope (run `pincer google setup` or set PINCER_GOOGLE_TOKEN_JSON).")
    return await factory.get("calendar")


async def _zone(svc: Any) -> ZoneInfo:
    cal = await asyncio.to_thread(lambda: svc.calendars().get(calendarId=CALENDAR_ID).execute())
    return ZoneInfo(str(cal["timeZone"]))


async def _tagged_events(svc: Any, tag: str) -> list[dict[str, Any]]:
    """Every event in the search window whose title starts with ``tag`` (filtered here,
    not by Google's free-text ``q``, whose tokenising of "Smoke-…" is not guaranteed)."""
    now = datetime.now(UTC)
    found: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": CALENDAR_ID,
            "timeMin": (now - timedelta(days=SEARCH_BEFORE_DAYS)).isoformat(),
            "timeMax": (now + timedelta(days=SEARCH_AFTER_DAYS)).isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = await asyncio.to_thread(svc.events().list(**kwargs).execute)
        found.extend(e for e in result.get("items", []) if is_tagged(e, tag))
        page_token = result.get("nextPageToken")
        if not page_token:
            return found


# ── Commands ───────────────────────────────────────────────────────────────────


def _checked_tag(tag: str) -> str:
    if not TAG_RE.match(tag):
        sys.exit(f"Refusing tag {tag!r}: expected {TAG_PREFIX}XXXXXX (6 hex digits), as printed by `plan`.")
    return tag


async def cmd_plan() -> int:
    svc = await _service()
    zone = await _zone(svc)
    today = datetime.now(zone).date()
    tomorrow, far_day = today + timedelta(days=1), today + timedelta(days=FAR_DAYS)
    tag = new_tag()
    print(f"Run tag: {tag}   calendar zone: {zone.key}   tomorrow: {tomorrow}   far day: {far_day}\n")
    print("Channel A = one chat with Pincer (e.g. WhatsApp); channel B = a different one (e.g. Telegram).")
    print("Send each message and wait for Pincer's reply before the next.\n")
    args = f"--tag {tag} --tomorrow {tomorrow} --far-day {far_day}"
    for step, channel, text in messages(tag, tomorrow, far_day):
        print(f"  [{step:>2}] channel {channel}: {text}")
    print("\nOrder: send 1, 2 and 6a; then record the IDs before the move and the rename:")
    print(f"  uv run python scripts/calendar_smoke.py snapshot {args} --out {tag.lower()}.json")
    print("Then send 3, 4, 5 and 6b (6b on channel B), and check:")
    print(f"  uv run python scripts/calendar_smoke.py check {args} --snapshot {tag.lower()}.json")
    print(f"  uv run python scripts/calendar_smoke.py cleanup --tag {tag}            # dry run")
    print(f"  uv run python scripts/calendar_smoke.py cleanup --tag {tag} --confirm  # delete")
    return 0


async def cmd_snapshot(tag: str, out: Path) -> int:
    svc = await _service()
    ids = snapshot_ids(await _tagged_events(svc, tag), tag)
    out.write_text(json.dumps(ids, indent=2) + "\n")
    print(f"Recorded {ids} → {out}")
    missing = {ALPHA, MEETING} - set(ids)
    if missing:
        print(f"WARNING: no single event found for {sorted(missing)} — send steps 1 and 6a first.")
        return 1
    return 0


async def cmd_check(tag: str, tomorrow: date, far_day: date, snapshot: Path | None) -> int:
    svc = await _service()
    zone = await _zone(svc)
    snap = json.loads(snapshot.read_text()) if snapshot else None
    rows = evaluate(await _tagged_events(svc, tag), zone, tag, tomorrow, far_day, snap)
    print(f"Run {tag} — calendar zone {zone.key}\n")
    print(render_table(rows))
    return 0 if all(r.status != "FAIL" for r in rows) else 1


async def cmd_cleanup(tag: str, confirm: bool) -> int:
    svc = await _service()
    zone = await _zone(svc)
    events = await _tagged_events(svc, tag)
    if not events:
        print(f"No events tagged {tag}.")
        return 0
    print(f"Events tagged {tag} ({len(events)}):")
    for e in events:
        print(f"  {e['id']}  {_wall_clock(e, zone)}  {e.get('summary')}")
    if not confirm:
        print("\nDry run — nothing deleted. Re-run with --confirm to delete exactly these events.")
        return 0
    for e in events:
        if not is_tagged(e, tag):  # belt and braces: never delete an untagged event
            continue
        await asyncio.to_thread(svc.events().delete(calendarId=CALENDAR_ID, eventId=e["id"]).execute)
        print(f"  deleted {e['id']}  {e.get('summary')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print a run tag, the dates and the messages to send")
    for name in ("snapshot", "check"):
        p = sub.add_parser(name)
        p.add_argument("--tag", required=True)
        p.add_argument("--tomorrow", required=True, type=date.fromisoformat)
        p.add_argument("--far-day", required=True, type=date.fromisoformat)
        if name == "snapshot":
            p.add_argument("--out", required=True, type=Path)
        else:
            p.add_argument("--snapshot", type=Path)
    p = sub.add_parser("cleanup", help="list (dry run) or delete the tagged events")
    p.add_argument("--tag", required=True)
    p.add_argument("--confirm", action="store_true", help="actually delete the listed events")
    args = parser.parse_args(argv)

    if args.command == "plan":
        return asyncio.run(cmd_plan())
    tag = _checked_tag(args.tag)
    if args.command == "snapshot":
        return asyncio.run(cmd_snapshot(tag, args.out))
    if args.command == "check":
        return asyncio.run(cmd_check(tag, args.tomorrow, args.far_day, args.snapshot))
    return asyncio.run(cmd_cleanup(tag, args.confirm))


if __name__ == "__main__":
    sys.exit(main())
