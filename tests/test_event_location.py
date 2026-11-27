"""Where an event is goes in LOCATION, which phone calendar apps show as a link
to the maps app. The place is the user's own words: nothing is looked up."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
from pim_tools.store import CalDAVStore, MemoryStore

icalendar = pytest.importorskip("icalendar")

DAY = {"from": "2026-12-03T00:00:00Z", "to": "2026-12-04T00:00:00Z"}


@pytest.fixture
def client():
    store = MemoryStore(tasks={})
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _add(client, **extra):
    body = {"title": "Lunch with Grandad", "start": "2026-12-03T13:00:00Z", **extra}
    return client.post("/events", json=body).json()["event"]


def test_a_place_is_kept_and_read_back(client):
    e = _add(client, location="  Steam,   Swindon ")
    assert e["location"] == "Steam, Swindon"
    listed = client.get("/events", params=DAY).json()["events"]
    assert listed[0]["location"] == "Steam, Swindon"


def test_no_place_means_no_location(client):
    assert _add(client)["location"] is None


def test_the_place_can_be_changed_cleared_or_left(client):
    e = _add(client, location="Steam, Swindon")
    ref = {"uid": e["uid"], "start": e["start"], "calendar": "personal"}
    moved = client.post("/events/change", json={**ref, "location": "The Plough, Wootton"})
    assert moved.status_code == 200 and moved.json()["changed"]
    assert moved.json()["event"]["location"] == "The Plough, Wootton"
    renamed = client.post("/events/change", json={**ref, "new_title": "Lunch with Gran too"})
    assert renamed.json()["event"]["location"] == "The Plough, Wootton"
    cleared = client.post("/events/change", json={**ref, "location": ""}).json()
    assert cleared["changed"] and cleared["event"]["location"] is None


# -- against the CalDAV store, with the server's own stored text


class _FakeCalendar:
    def __init__(self):
        self.saved: dict | None = None

    def save_event(self, **kwargs):
        self.saved = kwargs
        ev = icalendar.Event()
        for k, v in kwargs.items():
            ev.add(k, v)
        return type("Saved", (), {"icalendar_component": ev})()


def _store(cal=None) -> CalDAVStore:
    store = CalDAVStore.__new__(CalDAVStore)
    store._event_calendar_name = "personal"
    store._calendars = ("personal",)
    store._background = ()
    store._calendar = lambda name: cal  # type: ignore[method-assign]
    store.list_events = lambda start, end, calendar=None: []  # type: ignore[method-assign]
    return store


def test_caldav_writes_location_only_when_given():
    cal = _FakeCalendar()
    start = datetime(2026, 12, 3, 13, 0, tzinfo=UTC)
    e, _ = _store(cal).add_event("Lunch", start, None, None, None, "Steam, Swindon")
    assert cal.saved["location"] == "Steam, Swindon" and e.location == "Steam, Swindon"
    _store(cal).add_event("Dentist", start, None, None)
    assert "location" not in cal.saved


STORED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n"
    "UID:walk@test\r\nDTSTART;TZID=Europe/London:20261201T100000\r\n"
    "DTEND;TZID=Europe/London:20261201T110000\r\nRRULE:FREQ=WEEKLY\r\n"
    "SUMMARY:Walk\r\nLOCATION:Steam\\, Swindon\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_the_stored_escaped_comma_reads_back_plain():
    """Radicale stores 'Steam, Swindon' as LOCATION:Steam\\, Swindon."""
    ve = icalendar.Calendar.from_ical(STORED).walk("VEVENT")[0]
    assert CalDAVStore._as_event(ve, "personal", True).location == "Steam, Swindon"


class _Obj:
    def __init__(self, ical):
        self.icalendar_instance = ical
        self.saved = False

    def save(self):
        self.saved = True


def _one_day(location):
    """Change one day of a weekly series that has a location on its master."""
    ical = icalendar.Calendar.from_ical(STORED)
    master = ical.walk("VEVENT")[0]
    rid = datetime(2026, 12, 8, 10, 0, tzinfo=ZoneInfo("Europe/London"))
    obj = _Obj(ical)
    store = _store()
    store._locate = lambda c, u, s: (obj, ical, master, None, rid)  # type: ignore[method-assign]
    event, changed = store.change_event(
        "personal", "walk@test", rid, rid + timedelta(hours=1), None, None, None, location
    )
    day = next(c for c in ical.walk("VEVENT") if "RECURRENCE-ID" in c)
    return event, changed, day, master


def test_one_day_keeps_the_series_place_when_only_the_time_moves():
    event, changed, day, master = _one_day(None)
    assert changed and event.location == "Steam, Swindon"
    assert str(day["LOCATION"]) == "Steam, Swindon"


def test_one_day_can_be_somewhere_else_without_touching_the_series():
    event, _, day, master = _one_day("Coate Water")
    assert event.location == "Coate Water" and str(day["LOCATION"]) == "Coate Water"
    assert str(master["LOCATION"]) == "Steam, Swindon"
