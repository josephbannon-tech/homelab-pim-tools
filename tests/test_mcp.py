"""The MCP door, end to end: a real uvicorn on a loopback port and the MCP
SDK's own Streamable HTTP client. The transport streams, so the TestClient
is not enough here."""

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from pim_tools.app import app, get_store
from pim_tools.store import MemoryStore

TOKEN = "test-token"
TOOLS = {"list_tasks", "add_task", "complete_task", "list_events", "add_event"}


@pytest.fixture(scope="module")
def base_url():
    store = MemoryStore(tasks={"shopping": [], "chores": []})
    app.dependency_overrides[get_store] = lambda: store
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{url}/health")
            break
        except httpx.ConnectError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    app.dependency_overrides.clear()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("PIM_TOOLS_TOKEN", TOKEN)
    return {"Authorization": f"Bearer {TOKEN}"}


async def _with_session(url, headers, fn):
    async with httpx.AsyncClient(headers=headers) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def test_tools_are_the_operations(base_url, token):
    async def names(session):
        return {t.name for t in (await session.list_tools()).tools}

    assert asyncio.run(_with_session(base_url, token, names)) == TOOLS


def test_tool_call_writes_through_the_same_store(base_url, token):
    async def add(session):
        result = await session.call_tool("add_task", {"list": "shopping", "title": "Milk"})
        return result.isError, result.content[0].text

    is_error, text = asyncio.run(_with_session(base_url, token, add))
    assert not is_error
    assert '"created": true' in text
    listed = httpx.get(f"{base_url}/tasks/shopping", headers=token).json()
    assert [t["title"] for t in listed["tasks"]] == ["Milk"]


def test_mcp_rejects_missing_bearer(base_url, token):
    r = httpx.post(f"{base_url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
