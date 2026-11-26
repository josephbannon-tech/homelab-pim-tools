from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
from pim_tools.models import Event
from pim_tools.store import MemoryStore


@pytest.fixture
def client():
    store = MemoryStore(tasks={"shopping": [], "chores": []})
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_add_task_is_idempotent(client):
    r1 = client.post("/tasks", json={"list": "shopping", "title": "Milk"}).json()
    r2 = client.post("/tasks", json={"list": "shopping", "title": "  milk "}).json()
    assert r1["created"] is True
    assert r2["created"] is False
    assert r2["task"]["uid"] == r1["task"]["uid"]
    assert client.get("/tasks/shopping").json()["open"] == 1


def test_due_date_distinguishes_tasks(client):
    client.post("/tasks", json={"list": "chores", "title": "Bins"})
    r = client.post("/tasks", json={"list": "chores", "title": "Bins", "due": "2026-10-03"}).json()
    assert r["created"] is True
    assert r["task"]["due"] == "2026-10-03"


def test_complete_task_then_hidden(client):
    uid = client.post("/tasks", json={"list": "shopping", "title": "Eggs"}).json()["task"]["uid"]
    done = client.post("/tasks/complete", json={"list": "shopping", "uid": uid}).json()
    assert done["completed"] is True
    assert client.get("/tasks/shopping").json()["tasks"] == []
    assert client.get("/tasks/shopping?include_completed=true").json()["tasks"][0]["completed"]


def test_unknown_list_is_404(client):
    assert client.get("/tasks/nope").status_code == 404
    assert client.post("/tasks/complete", json={"list": "shopping", "uid": "x"}).status_code == 404


def test_add_event_defaults_and_idempotency(client):
    body = {"title": "Dentist", "start": "2026-10-02T18:00:00+01:00"}
    r1 = client.post("/events", json=body).json()
    r2 = client.post("/events", json=body).json()
    assert r1["created"] and not r2["created"]
    assert r1["event"]["end"].startswith("2026-10-02T19:00:00")
    listed = client.get(
        "/events", params={"from": "2026-10-02T00:00:00Z", "to": "2026-10-03T00:00:00Z"}
    )
    assert [e["title"] for e in listed.json()["events"]] == ["Dentist"]


def test_event_validation(client):
    bad = {"title": "X", "start": "2026-10-02T18:00:00Z", "end": "2026-10-02T17:00:00Z"}
    assert client.post("/events", json=bad).status_code == 422
    assert (
        client.get(
            "/events", params={"from": "2026-10-02T18:00:00Z", "to": "2026-10-02T17:00:00Z"}
        ).status_code
        == 422
    )


def test_openapi_operation_ids_are_the_tool_names(client):
    ops = {
        op["operationId"]
        for p in client.get("/openapi.json").json()["paths"].values()
        for op in p.values()
    }
    assert ops == {
        "list_tasks",
        "add_task",
        "complete_task",
        "list_events",
        "add_event",
        "change_event",
        "cancel_event",
        "list_notes",
        "read_note",
        "append_note",
        "list_recipes",
        "read_recipe",
        "read_cooking_guide",
        "add_recipe_note",
        "change_recipe_text",
        "set_recipe_tag",
        "save_meal_plan",
        "add_to_shopping",
    }


def test_memory_store_date_helpers():
    s = MemoryStore()
    s.add_event("A", datetime(2026, 1, 1, 9, tzinfo=UTC), None, None)
    assert s.list_events(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert s.add_task("l", "t", date(2026, 1, 1))[1]


def test_tracing_off_without_endpoint(monkeypatch):
    from pim_tools import telemetry

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.configure(app) is False


def test_routine_blocks_are_a_separate_calendar():
    from pim_tools.models import Event

    day = datetime(2026, 10, 2, tzinfo=UTC)
    store = MemoryStore(
        events=[
            Event(uid="a", title="Dentist", start=day.replace(hour=10)),
            Event(uid="b", title="Lunch", start=day.replace(hour=12), calendar="routine"),
        ]
    )
    app.dependency_overrides[get_store] = lambda: store
    c = TestClient(app)
    window = {"from": "2026-10-02T00:00:00Z", "to": "2026-10-03T00:00:00Z"}

    def titles(**extra):
        return [e["title"] for e in c.get("/events", params={**window, **extra}).json()["events"]]

    assert titles() == ["Dentist"]
    assert titles(calendar="routine") == ["Lunch"]
    assert titles(calendar="all") == ["Dentist", "Lunch"]
    assert c.get("/events", params={**window, "calendar": "work"}).status_code == 404
    app.dependency_overrides.clear()


def test_unknown_list_names_the_ones_that_exist(client):
    r = client.get("/tasks/hardware")
    assert r.status_code == 404
    assert "chores, shopping" in r.json()["detail"]


# ------------------------------------------------- changing one event, one day


def _series(store, title, days, hour=9, calendar="routine"):
    """A repeating series as the memory store holds it: one event a day, one uid."""
    for d in days:
        store.events.append(
            Event(
                uid="series-1",
                title=title,
                start=datetime(2026, 10, d, hour, 0, tzinfo=UTC),
                end=datetime(2026, 10, d, hour + 1, 0, tzinfo=UTC),
                calendar=calendar,
                recurring=True,
            )
        )


def _day(client, d, calendar="all"):
    r = client.get(
        "/events",
        params={
            "from": f"2026-10-{d:02d}T00:00:00Z",
            "to": f"2026-10-{d + 1:02d}T00:00:00Z",
            "calendar": calendar,
        },
    )
    return [(e["title"], e["start"][11:16], e["end"][11:16]) for e in r.json()["events"]]


def test_moving_one_day_of_a_series_leaves_the_other_days(client):
    store = app.dependency_overrides[get_store]()
    _series(store, "Coffee nap", [5, 6, 7], hour=17)
    ref = {"uid": "series-1", "start": "2026-10-06T17:00:00Z", "calendar": "routine"}
    r = client.post("/events/change", json={**ref, "new_start": "2026-10-06T17:30:00Z"}).json()
    assert r["changed"] and "only Tuesday 06 October" in r["scope"]
    assert _day(client, 6) == [("Coffee nap", "17:30", "18:30")]  # the length is kept
    assert _day(client, 5) == _day(client, 7) == [("Coffee nap", "17:00", "18:00")]


def test_shortening_renaming_and_notes(client):
    store = app.dependency_overrides[get_store]()
    _series(store, "Dinner", [5, 6], hour=18)
    ref = {"uid": "series-1", "start": "2026-10-05T18:00:00Z", "calendar": "routine"}
    body = {
        **ref,
        "new_end": "2026-10-05T18:40:00Z",
        "new_title": "Dinner: leftovers",
        "notes": "x",
    }
    r = client.post("/events/change", json=body).json()
    assert r["event"]["notes"] == "x"
    assert _day(client, 5) == [("Dinner: leftovers", "18:00", "18:40")]
    assert client.post("/events/change", json=body).json()["changed"] is False


def test_cancelling_one_day_skips_only_that_day(client):
    store = app.dependency_overrides[get_store]()
    _series(store, "Language", [5, 6, 7])
    ref = {"uid": "series-1", "start": "2026-10-06T09:00:00Z", "calendar": "routine"}
    r = client.post("/events/cancel", json=ref).json()
    assert r["cancelled"]["title"] == "Language" and "rest of the repeating series" in r["scope"]
    assert _day(client, 6) == [] and len(_day(client, 5)) == len(_day(client, 7)) == 1
    assert client.post("/events/cancel", json=ref).status_code == 404


def test_a_single_event_is_changed_or_removed_whole(client):
    made = client.post(
        "/events", json={"title": "Dentist", "start": "2026-10-02T10:00:00Z"}
    ).json()["event"]
    ref = {"uid": made["uid"], "start": made["start"]}
    r = client.post("/events/change", json={**ref, "new_start": "2026-10-02T11:00:00Z"}).json()
    assert r["scope"] == "this single event" and r["event"]["end"].startswith("2026-10-02T12:00")
    assert client.post("/events/cancel", json=ref).status_code == 404  # it moved
    moved = {"uid": made["uid"], "start": "2026-10-02T11:00:00Z"}
    assert client.post("/events/cancel", json=moved).json()["scope"] == "this single event"
    assert _day(client, 2) == []


def test_an_event_that_is_not_there_says_how_to_find_it(client):
    ref = {"uid": "nope", "start": "2026-10-02T10:00:00Z"}
    r = client.post("/events/change", json={**ref, "new_title": "x"})
    assert r.status_code == 404 and "list_events" in r.json()["detail"]
    assert client.post("/events/change", json=ref).status_code == 422  # nothing to change
    wrong = {**ref, "calendar": "holidays"}
    assert client.post("/events/cancel", json=wrong).status_code == 404


def test_an_end_before_the_start_is_refused(client):
    made = client.post(
        "/events", json={"title": "Dentist", "start": "2026-10-02T10:00:00Z"}
    ).json()["event"]
    body = {"uid": made["uid"], "start": made["start"], "new_end": "2026-10-02T09:00:00Z"}
    assert client.post("/events/change", json=body).status_code == 422
