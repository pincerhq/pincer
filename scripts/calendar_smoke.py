#!/usr/bin/env python3
"""Live calendar smoke check (calendar reliability, issue #297) — checklist + checker.

Replays the 2026-10-31 reproduction against a real Google account through a
running Pincer. YOU send the messages from your phone; this script prints them
and then reads the real calendar to check what Pincer actually stored. It never
talks to Pincer and never runs in CI (not a test module, not collected).

Each run has a unique tag (e.g. "Smoke-7F3A2C") that starts every title it
asks for ("Smoke-7F3A2C Alpha"). The script LISTS every event on the primary
calendar in a window around the run's dates (from the day before --tomorrow to
two days after --far-day) and keeps in memory only the events whose title
contains the tag; nothing else is stored, printed or written. Only titles that
START with "<tag> " (case-sensitive) count as the run's events and only those
are ever deleted; a title that merely contains the tag in another form
("Smoke-7F3A2C: Alpha") is a near miss: `check` FAILs and lists it, `cleanup`
lists it and leaves it alone. The window follows --tomorrow, so `check` and
`cleanup` still find the events when run days later. It uses the
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
    uv run python scripts/calendar_smoke.py cleanup --tag Smoke-7F3A2C --tomorrow 2026-11-02
    uv run python scripts/calendar_smoke.py cleanup --tag Smoke-7F3A2C --tomorrow 2026-11-02 --confirm

Exit code of `check`: 0 when every automatic row passes, 1 otherwise. A missing
or incomplete snapshot FAILs the ID rows. Rows marked MANUAL (Pincer's reply
wording, log lines) are for you to tick.
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
# The listing window around the run's dates: the day before --tomorrow to two days after --far-day.
WINDOW_BEFORE_DAYS = 1
WINDOW_AFTER_DAYS = 2
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


def mentions_tag(event: dict[str, Any], tag: str) -> bool:
    """The title contains the tag in any case — ours, or a near miss ("Smoke-7F3A2C: Alpha")."""
    return tag.lower() in str(event.get("summary") or "").lower()


def near_misses(events: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    """Events that mention the tag but are not ours: reported, never deleted."""
    return [e for e in events if mentions_tag(e, tag) and not is_tagged(e, tag)]


def _instant(event: dict[str, Any], key: str) -> datetime | None:
    raw = (event.get(key) or {}).get("dateTime")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo else None


def _start(event: dict[str, Any]) -> datetime | None:
    return _instant(event, "start")


def _named(events: list[dict[str, Any]], tag: str, name: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("summary") == title(tag, name) and e.get("status") != "cancelled"]


def _wall_clock(event: dict[str, Any], zone: ZoneInfo) -> str:
    start = _start(event)
    if start is None:
        return "(no start)"
    end = _instant(event, "end")
    span = f"{start.astimezone(zone):%Y-%m-%d %H:%M}"
    return f"{span}–{end.astimezone(zone):%H:%M}" if end else span


def _time_row(step: str, what: str, found: list[dict[str, Any]], zone: ZoneInfo, day: date, at: time) -> Row:
    want = datetime.combine(day, at, zone)
    want_end = want + timedelta(minutes=EVENT_MINUTES)
    want_text = f"{want:%Y-%m-%d %H:%M}–{want_end:%H:%M} {zone.key}"
    if len(found) != 1:
        return Row(step, f"{what} stored at {want_text}", "FAIL", f"{len(found)} matching event(s)")
    ok = _start(found[0]) == want and _instant(found[0], "end") == want_end
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
    near = near_misses(events, tag)
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
        Row(
            "all",
            "no near-miss titles (tag in another form; never deleted)",
            "FAIL" if near else "PASS",
            ", ".join(sorted(repr(str(e.get("summary"))) for e in near)) or "none",
        ),
        Row("1-6", "Pincer's replies quote the stored times and name the zone", "MANUAL", f"expect {zone.key}"),
        Row("1-6", "log: turn= and call= on every Tool call / Tool result line", "MANUAL", "grep 'Tool result'"),
        Row("1-6", "log: one calendar_write line per write", "MANUAL", "expect 5: 3 creates, the move, the rename"),
    ]
    return rows


def _id_row(step: str, what: str, found: list[dict[str, Any]], snapped: str | None) -> Row:
    if snapped is None:
        return Row(step, what, "FAIL", "no ID in the snapshot (run `snapshot` after steps 1, 2, 6a)")
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


def search_window(zone: ZoneInfo, tomorrow: date, far_day: date) -> tuple[datetime, datetime]:
    """The listing window: midnight of the day before ``tomorrow`` to the end of two days after ``far_day``."""
    first = min(tomorrow, far_day) - timedelta(days=WINDOW_BEFORE_DAYS)
    last = max(tomorrow, far_day) + timedelta(days=WINDOW_AFTER_DAYS + 1)
    return datetime.combine(first, time(0), zone), datetime.combine(last, time(0), zone)


async def _run_events(svc: Any, tag: str, window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
    """Lists every event in ``window`` and keeps only those whose title mentions ``tag`` (filtered
    here, not by Google's free-text ``q``, whose tokenising of "Smoke-…" is not guaranteed)."""
    found: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": CALENDAR_ID,
            "timeMin": window[0].astimezone(UTC).isoformat(),
            "timeMax": window[1].astimezone(UTC).isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = await asyncio.to_thread(svc.events().list(**kwargs).execute)
        found.extend(e for e in result.get("items", []) if mentions_tag(e, tag))
        page_token = result.get("nextPageToken")
        if not page_token:
            return found


# ── Commands ───────────────────────────────────────────────────────────────────


def _checked_tag(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
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
    cleanup = f"uv run python scripts/calendar_smoke.py cleanup --tag {tag} --tomorrow {tomorrow}"
    print(f"  {cleanup}            # dry run")
    print(f"  {cleanup} --confirm  # delete")
    return 0


async def cmd_snapshot(tag: str, tomorrow: date, far_day: date, out: Path) -> int:
    svc = await _service()
    zone = await _zone(svc)
    ids = snapshot_ids(await _run_events(svc, tag, search_window(zone, tomorrow, far_day)), tag)
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
    events = await _run_events(svc, tag, search_window(zone, tomorrow, far_day))
    rows = evaluate(events, zone, tag, tomorrow, far_day, snap)
    print(f"Run {tag} — calendar zone {zone.key}\n")
    print(render_table(rows))
    return 0 if all(r.status != "FAIL" for r in rows) else 1


async def cmd_cleanup(tag: str, tomorrow: date, far_day: date, confirm: bool) -> int:
    svc = await _service()
    zone = await _zone(svc)
    start, end = search_window(zone, tomorrow, far_day)
    listed = await _run_events(svc, tag, (start, end))
    events = [e for e in listed if is_tagged(e, tag)]
    near = near_misses(listed, tag)
    print(f"Searched {start:%Y-%m-%d} to {end - timedelta(days=1):%Y-%m-%d} ({zone.key}).")
    if near:
        print(f"Near misses — NOT deleted, check and remove them by hand ({len(near)}):")
        for e in near:
            print(f"  {e['id']}  {_wall_clock(e, zone)}  {e.get('summary')}")
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
    p.add_argument("--tomorrow", required=True, type=date.fromisoformat, help="the run's tomorrow, as `plan` printed")
    p.add_argument(
        "--far-day",
        type=date.fromisoformat,
        help=f"the run's far day (default: --tomorrow + {FAR_DAYS - 1} days, as `plan` prints it)",
    )
    p.add_argument("--confirm", action="store_true", help="actually delete the listed events")
    args = parser.parse_args(argv)

    if args.command == "plan":
        return asyncio.run(cmd_plan())
    tag = _checked_tag(args.tag)
    if args.command == "snapshot":
        return asyncio.run(cmd_snapshot(tag, args.tomorrow, args.far_day, args.out))
    if args.command == "check":
        return asyncio.run(cmd_check(tag, args.tomorrow, args.far_day, args.snapshot))
    far_day = args.far_day or args.tomorrow + timedelta(days=FAR_DAYS - 1)
    return asyncio.run(cmd_cleanup(tag, args.tomorrow, far_day, args.confirm))


if __name__ == "__main__":
    sys.exit(main())
