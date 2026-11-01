"""
The live smoke checker (scripts/calendar_smoke.py) against the fake Calendar.

Slice F's evidence is this script's PASS/FAIL table, so a false PASS is the
failure that matters: a wrong end time, a duplicate with a mangled title, a
snapshot missing an ID, or a run checked days later must each FAIL.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from integrations.google.fake_calendar import FakeCalendarService

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "calendar_smoke.py"
ZONE = "Europe/Kiev"
TAG = "Smoke-7F3A2C"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("calendar_smoke_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load()


def _today() -> date:
    return datetime.now(ZoneInfo(ZONE)).date()


@pytest.fixture
def fake() -> FakeCalendarService:
    return FakeCalendarService({"primary": ZONE})


@pytest.fixture
def served(monkeypatch, fake) -> FakeCalendarService:
    """The script's Google service is the fake."""

    async def _service() -> FakeCalendarService:
        return fake

    monkeypatch.setattr(smoke, "_service", _service)
    return fake


def _at(day: date, hour: int, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(ZONE)).isoformat()


def _seed(fake: FakeCalendarService, name: str, day: date, start: int, end: int) -> str:
    return fake.seed_event("primary", f"{TAG} {name}", _at(day, start), _at(day, end))


def _good_run(fake: FakeCalendarService, tomorrow: date, far_day: date) -> dict[str, str]:
    """What a correct run leaves: Alpha moved to 17:00, Beta 12 days out, Meeting renamed in place."""
    alpha = _seed(fake, "Alpha", tomorrow, 17, 18)
    _seed(fake, "Beta", far_day, 15, 16)
    meeting = _seed(fake, "Meeting (updated)", tomorrow, 10, 11)
    return {"Alpha": alpha, "Meeting": meeting}


def _dates(today: date | None = None) -> tuple[date, date]:
    today = today or _today()
    return today + timedelta(days=1), today + timedelta(days=smoke.FAR_DAYS)


def _rows(fake: FakeCalendarService, tomorrow: date, far_day: date, snapshot: dict[str, str] | None):
    return smoke.evaluate(fake.events_on("primary"), ZoneInfo(ZONE), TAG, tomorrow, far_day, snapshot)


def _row(rows, starts: str):
    (row,) = [r for r in rows if r.check.startswith(starts)]
    return row


def _check(tmp_path: Path, tomorrow: date, far_day: date, snapshot: dict[str, str] | None) -> int:
    argv = ["check", "--tag", TAG, "--tomorrow", tomorrow.isoformat(), "--far-day", far_day.isoformat()]
    if snapshot is not None:
        path = tmp_path / "snap.json"
        path.write_text(json.dumps(snapshot))
        argv += ["--snapshot", str(path)]
    return smoke.main(argv)


def test_a_good_run_passes_every_automatic_row(served, tmp_path, capsys) -> None:
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    rows = _rows(served, tomorrow, far_day, snap)
    assert [r.status for r in rows if r.status != "MANUAL"] == ["PASS"] * 9
    assert _check(tmp_path, tomorrow, far_day, snap) == 0
    assert "9/9 automatic checks passed; 3 manual." in capsys.readouterr().out


def test_alpha_moved_with_the_wrong_end_fails(served, tmp_path) -> None:
    """S1: 17:00–23:00 is not the requested 17:00–18:00 (the end must move with the start)."""
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    alpha = served.event("primary", snap["Alpha"])
    alpha["end"] = {"dateTime": _at(tomorrow, 23)}
    row = _row(_rows(served, tomorrow, far_day, snap), f'"{TAG} Alpha" (moved) stored at')
    assert row.status == "FAIL", row
    assert row.detail.endswith("17:00–23:00"), row
    assert _check(tmp_path, tomorrow, far_day, snap) == 1


def test_a_mangled_duplicate_fails_and_is_listed(served, tmp_path, capsys) -> None:
    """S2: "Smoke-7F3A2C: Alpha" is not ours by the exact prefix, but it is a duplicate."""
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    served.seed_event("primary", f"{TAG}: Alpha", _at(tomorrow, 15), _at(tomorrow, 16))
    served.seed_event("primary", f"Copy of {TAG.lower()} Meeting", _at(tomorrow, 10), _at(tomorrow, 11))
    row = _row(_rows(served, tomorrow, far_day, snap), "no near-miss titles")
    assert row.status == "FAIL", row
    assert f"{TAG}: Alpha" in row.detail and f"Copy of {TAG.lower()} Meeting" in row.detail, row
    assert _check(tmp_path, tomorrow, far_day, snap) == 1
    assert f"{TAG}: Alpha" in capsys.readouterr().out


def test_cleanup_never_deletes_a_near_miss(served) -> None:
    tomorrow, far_day = _dates()
    _good_run(served, tomorrow, far_day)
    near = served.seed_event("primary", f"{TAG}: Alpha", _at(tomorrow, 15), _at(tomorrow, 16))
    other = served.seed_event("primary", "Dentist", _at(tomorrow, 12), _at(tomorrow, 13))
    assert smoke.main(["cleanup", "--tag", TAG, "--tomorrow", tomorrow.isoformat(), "--confirm"]) == 0
    assert sorted(e["id"] for e in served.events_on("primary")) == sorted([near, other])


@pytest.mark.parametrize("missing", ["Alpha", "Meeting"])
def test_a_snapshot_missing_an_id_fails(served, tmp_path, missing) -> None:
    """S3: an ID that was never recorded cannot be shown unchanged."""
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    del snap[missing]
    rows = _rows(served, tomorrow, far_day, snap)
    id_rows = [r for r in rows if "ID" in r.check]
    assert [r.status for r in id_rows] == (["FAIL", "PASS"] if missing == "Alpha" else ["PASS", "FAIL"])
    assert "MANUAL" not in {r.status for r in id_rows}
    assert _check(tmp_path, tomorrow, far_day, snap) == 1


def test_no_snapshot_fails_the_id_rows(served, tmp_path) -> None:
    tomorrow, far_day = _dates()
    _good_run(served, tomorrow, far_day)
    rows = _rows(served, tomorrow, far_day, None)
    assert [r.status for r in rows if "ID" in r.check] == ["FAIL", "FAIL"]
    assert _check(tmp_path, tomorrow, far_day, None) == 1


def test_check_and_cleanup_find_a_run_days_later(served, tmp_path, capsys) -> None:
    """S4: the window follows the run's dates, not today's clock."""
    tomorrow, far_day = _dates(_today() - timedelta(days=20))
    snap = _good_run(served, tomorrow, far_day)
    assert _check(tmp_path, tomorrow, far_day, snap) == 0
    assert smoke.main(["cleanup", "--tag", TAG, "--tomorrow", tomorrow.isoformat()]) == 0
    out = capsys.readouterr().out
    assert f"Events tagged {TAG} (3):" in out
    assert len(served.events_on("primary")) == 3  # dry run


def test_a_tag_with_a_trailing_newline_is_refused() -> None:
    with pytest.raises(SystemExit):
        smoke._checked_tag(f"{TAG}\n")
