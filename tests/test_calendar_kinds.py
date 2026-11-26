"""Colour by calendar: events are filed on kind calendars (each with its own
colour on the phone), and moved between them to change colour."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
from pim_tools.models import Event
from pim_tools.store import MemoryStore

DAY = datetime(2026, 12, 2, tzinfo=UTC)
CALS = ("personal", "social", "health", "routine")


@pytest.fixture()
def store():
    s = MemoryStore(
        calendars=CALS,
        colours={
            "personal": "#33b679",
            "social": "#8e44ad",
            "health": "#d50000",
            "routine": "#ff7a1f",
        },
        events=[
            Event(uid="d", title="Dentist", start=DAY.replace(hour=10)),
            Event(uid="g", title="Gig", start=DAY.replace(hour=20), calendar="social"),
            Event(
                uid="w", title="Work", start=DAY.replace(hour=9), calendar="routine", recurring=True
            ),
            Event(
                uid="w",
                title="Work",
                start=DAY.replace(day=3, hour=9),
                calendar="routine",
                recurring=True,
            ),
        ],
    )
    app.dependency_overrides[get_store] = lambda: s
    yield s
    app.dependency_overrides.clear()


@pytest.fixture()
def c(store):
    return TestClient(app)


WINDOW = {"from": "2026-12-02T00:00:00Z", "to": "2026-12-04T00:00:00Z"}


def _titles(c, **extra):
    return [e["title"] for e in c.get("/events", params={**WINDOW, **extra}).json()["events"]]


def test_default_read_covers_every_appointment_calendar(c):
    assert _titles(c) == ["Dentist", "Gig"]
    assert _titles(c, calendar="social") == ["Gig"]
    assert _titles(c, calendar="routine") == ["Work", "Work"]


def test_list_calendars_gives_colours_and_roles(c):
    cals = {x["name"]: x for x in c.get("/calendars").json()["calendars"]}
    assert list(cals) == list(CALS)
    assert cals["social"]["colour"] == "#8e44ad"
    assert cals["personal"]["default"] and not cals["social"]["default"]
    assert cals["routine"]["routine"] and not cals["health"]["routine"]


def test_add_event_to_a_kind_calendar(c, store):
    r = c.post(
        "/events", json={"title": "Physio", "start": "2026-12-03T11:00:00Z", "calendar": "health"}
    )
    assert r.status_code == 200 and r.json()["event"]["calendar"] == "health"
    r = c.post(
        "/events", json={"title": "X", "start": "2026-12-03T11:00:00Z", "calendar": "holidays"}
    )
    assert r.status_code == 404 and "'social'" in r.json()["detail"]


def test_add_event_never_onto_routine(c):
    r = c.post(
        "/events", json={"title": "X", "start": "2026-12-03T11:00:00Z", "calendar": "routine"}
    )
    assert r.status_code == 422


def test_add_event_default_calendar_unchanged(c):
    r = c.post("/events", json={"title": "Call", "start": "2026-12-03T15:00:00Z"})
    assert r.json()["event"]["calendar"] == "personal"


def test_move_single_event(c, store):
    r = c.post(
        "/events/move",
        json={
            "uid": "d",
            "start": "2026-12-02T10:00:00Z",
            "calendar": "personal",
            "to_calendar": "health",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["event"]["calendar"] == "health" and body["from_calendar"] == "personal"
    assert _titles(c, calendar="health") == ["Dentist"]
    assert _titles(c, calendar="personal") == []


def test_move_one_day_of_routine_leaves_the_series(c, store):
    r = c.post(
        "/events/move",
        json={
            "uid": "w",
            "start": "2026-12-02T09:00:00Z",
            "calendar": "routine",
            "to_calendar": "health",
        },
    )
    assert r.status_code == 200 and "only" in r.json()["scope"]
    assert r.json()["event"]["uid"] != "w"
    assert _titles(c, calendar="routine") == ["Work"]  # the 3rd stays
    assert _titles(c, calendar="health") == ["Work"]


def test_whole_routine_series_cannot_move(c):
    r = c.post(
        "/events/move",
        json={
            "uid": "w",
            "start": "2026-12-02T09:00:00Z",
            "calendar": "routine",
            "to_calendar": "health",
            "whole_series": True,
        },
    )
    assert r.status_code == 422 and "one day" in r.json()["detail"]


def test_nothing_moves_onto_routine_or_onto_itself(c):
    base = {"uid": "d", "start": "2026-12-02T10:00:00Z", "calendar": "personal"}
    assert c.post("/events/move", json={**base, "to_calendar": "routine"}).status_code == 422
    assert c.post("/events/move", json={**base, "to_calendar": "personal"}).status_code == 422
    r = c.post("/events/move", json={**base, "to_calendar": "holidays"})
    assert r.status_code == 404 and "calendars:" in r.json()["detail"]
    r = c.post("/events/move", json={**base, "uid": "nope", "to_calendar": "health"})
    assert r.status_code == 404 and "list_events" in r.json()["detail"]
