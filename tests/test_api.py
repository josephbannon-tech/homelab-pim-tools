from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
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
    assert ops == {"list_tasks", "add_task", "complete_task", "list_events", "add_event"}


def test_memory_store_date_helpers():
    s = MemoryStore()
    s.add_event("A", datetime(2026, 1, 1, 9, tzinfo=UTC), None, None)
    assert s.list_events(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))
    assert s.add_task("l", "t", date(2026, 1, 1))[1]


def test_tracing_off_without_endpoint(monkeypatch):
    from pim_tools import telemetry

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.configure(app) is False
