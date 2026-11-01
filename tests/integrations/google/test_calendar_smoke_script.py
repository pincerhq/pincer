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
    """What a correct run leaves: Alpha moved to 17:00, Beta 12 days out, Meeting and Kunde renamed in place."""
    alpha = _seed(fake, "Alpha", tomorrow, 17, 18)
    _seed(fake, "Beta", far_day, 15, 16)
    meeting = _seed(fake, "Meeting (updated)", tomorrow, 10, 11)
    kunde = _seed(fake, "Kunde neu", tomorrow, 12, 13)
    return {"Alpha": alpha, "Meeting": meeting, "Kunde": kunde}


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
    assert [r.status for r in rows if r.status != "MANUAL"] == ["PASS"] * 13
    assert _check(tmp_path, tomorrow, far_day, snap) == 0
    assert "13/13 automatic checks passed; 3 manual." in capsys.readouterr().out


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


async def test_the_delete_step_re_checks_every_event_it_is_handed(served, capsys) -> None:
    """Defence in depth: even a list that wrongly holds a near miss deletes only tagged events."""
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    near = served.seed_event("primary", f"{TAG}: Alpha", _at(tomorrow, 15), _at(tomorrow, 16))
    handed = [served.event("primary", near), served.event("primary", snap["Alpha"])]

    await smoke._delete_ours(served, handed, TAG)

    assert [e["id"] for e in served.events_on("primary") if e["id"] in (near, snap["Alpha"])] == [near]
    assert f"skipped {near} (not tagged {TAG})" in capsys.readouterr().out


def test_plan_says_what_the_script_cannot_see(served, monkeypatch, capsys) -> None:
    monkeypatch.setattr(smoke, "new_tag", lambda: TAG)
    assert smoke.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "invisible to this script" in out
    assert "dropped the tag" in out and "outside the window" in out


@pytest.mark.parametrize("missing", ["Alpha", "Meeting", "Kunde"])
def test_a_snapshot_missing_an_id_fails(served, tmp_path, missing) -> None:
    """S3: an ID that was never recorded cannot be shown unchanged."""
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    del snap[missing]
    rows = _rows(served, tomorrow, far_day, snap)
    id_rows = [r for r in rows if "ID" in r.check]
    expected = {
        "Alpha": ["FAIL", "PASS", "PASS"],
        "Meeting": ["PASS", "FAIL", "PASS"],
        "Kunde": ["PASS", "PASS", "FAIL"],
    }
    assert [r.status for r in id_rows] == expected[missing]
    assert "MANUAL" not in {r.status for r in id_rows}
    assert _check(tmp_path, tomorrow, far_day, snap) == 1


def test_no_snapshot_fails_the_id_rows(served, tmp_path) -> None:
    tomorrow, far_day = _dates()
    _good_run(served, tomorrow, far_day)
    rows = _rows(served, tomorrow, far_day, None)
    assert [r.status for r in rows if "ID" in r.check] == ["FAIL", "FAIL", "FAIL"]
    assert _check(tmp_path, tomorrow, far_day, None) == 1


def test_check_and_cleanup_find_a_run_days_later(served, tmp_path, capsys) -> None:
    """S4: the window follows the run's dates, not today's clock."""
    tomorrow, far_day = _dates(_today() - timedelta(days=20))
    snap = _good_run(served, tomorrow, far_day)
    assert _check(tmp_path, tomorrow, far_day, snap) == 0
    assert smoke.main(["cleanup", "--tag", TAG, "--tomorrow", tomorrow.isoformat()]) == 0
    out = capsys.readouterr().out
    assert f"Events tagged {TAG} (4):" in out
    assert len(served.events_on("primary")) == 4  # dry run


def test_a_german_rename_that_created_a_second_event_fails(served, tmp_path, capsys) -> None:
    """Step 7: "Benenne das Meeting … um" must rename Kunde in place, not add "Kunde neu" beside it."""
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    served.event("primary", snap["Kunde"])["summary"] = f"{TAG} Kunde"  # the original, never renamed
    _seed(served, "Kunde neu", tomorrow, 12, 13)  # a second event under the new title
    rows = _rows(served, tomorrow, far_day, snap)
    step7 = [(r.check, r.status, r.detail) for r in rows if r.step == "7"]
    assert step7[:3] == [
        (f'exactly one "{TAG} Kunde neu" after the German rename', "PASS", "1 event(s)"),
        (f'no "{TAG} Kunde" left under the old title', "FAIL", "1 event(s)"),
        (
            "renamed Kunde keeps its original ID",
            "FAIL",
            f"now {_named_id(served, 'Kunde neu')}, snapshot {snap['Kunde']}",
        ),
    ]
    assert _row(rows, "no other tagged events").status == "FAIL"
    assert _check(tmp_path, tomorrow, far_day, snap) == 1
    assert "10/13 automatic checks passed; 3 manual." in capsys.readouterr().out


def test_a_good_german_rename_passes_the_step_7_rows(served) -> None:
    tomorrow, far_day = _dates()
    snap = _good_run(served, tomorrow, far_day)
    rows = [r for r in _rows(served, tomorrow, far_day, snap) if r.step == "7"]
    assert [(r.status, r.detail) for r in rows] == [
        ("PASS", "1 event(s)"),
        ("PASS", "0 event(s)"),
        ("PASS", f"now {snap['Kunde']}, snapshot {snap['Kunde']}"),
        ("PASS", f"{tomorrow.isoformat()} 12:00–13:00"),
    ]
    assert rows[3].check == f'"{TAG} Kunde neu" time unchanged stored at {tomorrow.isoformat()} 12:00–13:00 {ZONE}'


def test_snapshot_records_the_german_event(served, tmp_path) -> None:
    tomorrow, far_day = _dates()
    alpha = _seed(served, "Alpha", tomorrow, 15, 16)
    _seed(served, "Beta", far_day, 15, 16)
    meeting = _seed(served, "Meeting", tomorrow, 10, 11)
    out = tmp_path / "snap.json"
    argv = ["snapshot", "--tag", TAG, "--tomorrow", tomorrow.isoformat(), "--far-day", far_day.isoformat()]
    assert smoke.main([*argv, "--out", str(out)]) == 1  # Kunde not created yet
    kunde = _seed(served, "Kunde", tomorrow, 12, 13)
    assert smoke.main([*argv, "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == {"Alpha": alpha, "Meeting": meeting, "Kunde": kunde}


def test_plan_prints_the_german_rename(served, monkeypatch, capsys) -> None:
    monkeypatch.setattr(smoke, "new_tag", lambda: TAG)
    tomorrow = _today() + timedelta(days=1)
    assert smoke.main(["plan"]) == 0
    out = capsys.readouterr().out
    assert (
        f'[7a] channel A: Erstelle morgen ({tomorrow.isoformat()}) um 12:00 einen Termin "{TAG} Kunde" für eine Stunde.'
        in out
    )
    assert f'[7b] channel A: Benenne das Meeting "{TAG} Kunde" in "{TAG} Kunde neu" um.' in out
    assert "send 1, 2, 6a and 7a" in out


def _named_id(fake: FakeCalendarService, name: str) -> str:
    (event,) = [e for e in fake.events_on("primary") if e.get("summary") == f"{TAG} {name}"]
    return str(event["id"])


def test_a_tag_with_a_trailing_newline_is_refused() -> None:
    with pytest.raises(SystemExit):
        smoke._checked_tag(f"{TAG}\n")
