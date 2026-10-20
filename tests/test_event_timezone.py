"""Events must reach the server as UTC. A fixed-offset datetime (the normal
result of parsing an ISO string with +01:00) serialises in iCalendar as
DTSTART;TZID="+01:00", which names no VTIMEZONE; clients then guess the zone."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from pim_tools.store import CalDAVStore

icalendar = pytest.importorskip("icalendar")


class _FakeCalendar:
    def __init__(self):
        self.saved: dict | None = None

    def save_event(self, **kwargs):
        self.saved = kwargs
        ev = icalendar.Event()
        for k, v in kwargs.items():
            ev.add(k, v)
        return type("Saved", (), {"icalendar_component": ev})()


def _store_with(cal: _FakeCalendar) -> CalDAVStore:
    store = CalDAVStore.__new__(CalDAVStore)
    store._event_calendar_name = "personal"
    store._calendar = lambda name: cal  # type: ignore[method-assign]
    store.list_events = lambda start, end: []  # type: ignore[method-assign]
    return store


def test_fixed_offset_start_is_written_as_utc():
    cal = _FakeCalendar()
    start = datetime(2026, 10, 22, 10, 0, tzinfo=timezone(timedelta(hours=1)))
    event, created = _store_with(cal).add_event("Dentist", start, None, None)

    assert created
    assert cal.saved["dtstart"].tzinfo is UTC
    assert cal.saved["dtstart"] == start  # same instant
    assert cal.saved["dtend"] == start + timedelta(hours=1)
    ical = icalendar.Event()
    ical.add("dtstart", cal.saved["dtstart"])
    ical.add("dtend", cal.saved["dtend"])
    text = ical.to_ical().decode()
    assert "TZID" not in text
    assert "DTSTART:20261022T090000Z" in text
    assert event.start == start


def test_naive_start_is_treated_as_utc():
    cal = _FakeCalendar()
    _store_with(cal).add_event("X", datetime(2026, 10, 22, 9, 0), None, None)
    assert cal.saved["dtstart"] == datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
