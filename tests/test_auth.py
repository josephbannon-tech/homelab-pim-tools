"""The bearer gate. Open when PIM_TOOLS_TOKEN is unset (v1 behaviour), closed
on every route except the probe and the spec when it is set."""

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
from pim_tools.store import MemoryStore

TOKEN = "test-token"


@pytest.fixture
def client():
    store = MemoryStore(tasks={"shopping": [], "chores": []})
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_open_when_no_token_configured(client, monkeypatch):
    monkeypatch.delenv("PIM_TOOLS_TOKEN", raising=False)
    assert client.get("/tasks/shopping").status_code == 200


def test_closed_when_token_configured(client, monkeypatch):
    monkeypatch.setenv("PIM_TOOLS_TOKEN", TOKEN)
    r = client.get("/tasks/shopping")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    wrong = {"Authorization": "Bearer nope"}
    assert client.get("/tasks/shopping", headers=wrong).status_code == 401
    assert client.post("/tasks", json={"list": "shopping", "title": "Milk"}).status_code == 401
    assert client.post("/mcp", json={}).status_code == 401


def test_right_token_passes(client, monkeypatch):
    monkeypatch.setenv("PIM_TOOLS_TOKEN", TOKEN)
    ok = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/tasks/shopping", headers=ok).status_code == 200


def test_probe_and_spec_stay_open(client, monkeypatch):
    monkeypatch.setenv("PIM_TOOLS_TOKEN", TOKEN)
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
