"""The eval's own pim-tools: the real app on a loopback port over a recording
in-memory store.

Scoring happens here, at the tool server, because a chat front end that runs
its own tool loop (headless Claude Code over MCP, for one) returns only text.
What reached the store is the ground truth for both doors, REST and MCP.
A call rejected by request validation never reaches the store and is not
recorded; it changed nothing, and the model saw the error.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import uvicorn

from pim_tools.app import app, get_store
from pim_tools.models import Event, Task
from pim_tools.store import MemoryStore, NotFound

WRITES = frozenset({"add_task", "complete_task", "add_event"})


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    ok: bool = True
    changed: bool = False  # a write that altered the store

    def as_json(self) -> dict[str, Any]:
        args = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in self.args.items()}
        return {"tool": self.tool, "args": args, "ok": self.ok, "changed": self.changed}


@dataclass
class RecordingStore(MemoryStore):
    """MemoryStore plus a call log. Unknown task lists raise NotFound on add,
    as the CalDAV store does (MemoryStore alone would create the list)."""

    calls: list[Call] = field(default_factory=list)

    def _record(self, tool: str, args: dict[str, Any]) -> Call:
        call = Call(tool, args)
        self.calls.append(call)
        return call

    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]:
        call = self._record(
            "list_tasks", {"list": list_name, "include_completed": include_completed}
        )
        try:
            return super().list_tasks(list_name, include_completed)
        except NotFound:
            call.ok = False
            raise

    def add_task(self, list_name: str, title: str, due: date | None) -> tuple[Task, bool]:
        call = self._record("add_task", {"list": list_name, "title": title, "due": due})
        if list_name not in self.tasks:
            call.ok = False
            raise NotFound(list_name)
        task, created = super().add_task(list_name, title, due)
        call.changed = created
        return task, created

    def complete_task(self, list_name: str, uid: str) -> Task:
        call = self._record("complete_task", {"list": list_name, "uid": uid})
        try:
            already = next(
                (t.completed for t in self.tasks.get(list_name, []) if t.uid == uid), False
            )
            task = super().complete_task(list_name, uid)
        except NotFound:
            call.ok = False
            raise
        call.changed = not already
        return task

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        self._record("list_events", {"from": start, "to": end})
        return super().list_events(start, end)

    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]:
        call = self._record(
            "add_event", {"title": title, "start": start, "end": end, "notes": notes}
        )
        event, created = super().add_event(title, start, end, notes)
        call.changed = created
        return event, created


def seeded_store(seed: dict[str, Any]) -> RecordingStore:
    """Build a store from a resolved seed: {tasks: {list: [..]}, events: [..]}.
    A task is a title or {title, due, completed}; uids are opaque on purpose so
    a model has to read them from list_tasks."""
    store = RecordingStore()
    for list_name, items in (seed.get("tasks") or {}).items():
        store.tasks[list_name] = []
        for item in items:
            item = {"title": item} if isinstance(item, str) else item
            store.tasks[list_name].append(
                Task(
                    uid=str(uuid.uuid4()),
                    list=list_name,
                    title=item["title"],
                    due=date.fromisoformat(item["due"]) if item.get("due") else None,
                    completed=bool(item.get("completed")),
                )
            )
    for ev in seed.get("events") or []:
        store.events.append(
            Event(
                uid=str(uuid.uuid4()),
                title=ev["title"],
                start=datetime.fromisoformat(ev["start"]),
                end=datetime.fromisoformat(ev["end"]) if ev.get("end") else None,
                notes=ev.get("notes"),
            )
        )
    return store


class EvalServer:
    """One uvicorn for the whole run; each case swaps in a fresh store."""

    def __init__(self) -> None:
        self.store = RecordingStore()
        self.url = ""
        self._server: uvicorn.Server | None = None

    def __enter__(self) -> EvalServer:
        # The loopback instance is open: no token, nothing but this run can reach it.
        os.environ.pop("PIM_TOOLS_TOKEN", None)
        app.dependency_overrides[get_store] = lambda: self.store
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        threading.Thread(target=self._server.run, daemon=True).start()
        self.url = f"http://127.0.0.1:{port}"
        for _ in range(200):
            try:
                httpx.get(f"{self.url}/health")
                return self
            except httpx.ConnectError:
                time.sleep(0.05)
        raise RuntimeError("eval server did not start")

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.should_exit = True
        app.dependency_overrides.pop(get_store, None)

    def reset(self, seed: dict[str, Any]) -> RecordingStore:
        self.store = seeded_store(seed)
        return self.store
