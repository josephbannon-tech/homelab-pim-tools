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
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from pim_tools.app import app, get_notes, get_recipes, get_store
from pim_tools.models import Event, Task
from pim_tools.notes import Notes
from pim_tools.recipes import Recipes
from pim_tools.store import MemoryStore, NotFound

WRITES = frozenset(
    {
        "add_task",
        "complete_task",
        "add_event",
        "change_event",
        "cancel_event",
        "append_note",
        "add_recipe_note",
        "change_recipe_text",
        "set_recipe_tag",
        "save_meal_plan",
    }
)


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
    seeded_titles: dict[str, str] = field(default_factory=dict)  # event uid -> title at seed

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
            if uid in self.jobs:  # a repeating job not due is already done for now
                already = not self.jobs[uid].is_due(self.clock())
            task = super().complete_task(list_name, uid)
        except NotFound:
            call.ok = False
            raise
        call.changed = not already
        return task

    def list_events(
        self, start: datetime, end: datetime, calendar: str | None = None
    ) -> list[Event]:
        self._record("list_events", {"from": start, "to": end, "calendar": calendar or "personal"})
        return super().list_events(start, end, calendar)

    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]:
        call = self._record(
            "add_event", {"title": title, "start": start, "end": end, "notes": notes}
        )
        event, created = super().add_event(title, start, end, notes)
        call.changed = created
        return event, created

    def change_event(self, calendar, uid, start, new_start, new_end, new_title, notes):
        call = self._record(
            "change_event",
            {
                "calendar": calendar,
                "uid": uid,
                "start": start,
                "new_start": new_start,
                "new_end": new_end,
                "new_title": new_title,
                "notes": notes,
            },
        )
        try:
            event, changed = super().change_event(
                calendar, uid, start, new_start, new_end, new_title, notes
            )
        except Exception:
            call.ok = False
            raise
        call.changed = changed
        return event, changed

    def cancel_event(self, calendar, uid, start):
        call = self._record("cancel_event", {"calendar": calendar, "uid": uid, "start": start})
        try:
            gone = super().cancel_event(calendar, uid, start)
        except Exception:
            call.ok = False
            raise
        call.changed = True
        return gone


class RecordingNotes(Notes):
    """The notes folder, writing its calls into the store's log so one list
    holds everything a turn did."""

    def __init__(self, root, calls: list[Call]):
        super().__init__(root, exclude=("Private",))
        self.calls = calls

    def _run(self, tool: str, args: dict[str, Any], fn, write: bool = False):
        call = Call(tool, args)
        self.calls.append(call)
        try:
            out = fn()
        except Exception:
            call.ok = False
            raise
        call.changed = write
        return out

    def list(self, query=None):
        return self._run("list_notes", {"query": query}, lambda: Notes.list(self, query))

    def read(self, name):
        return self._run("read_note", {"name": name}, lambda: Notes.read(self, name))

    def append(self, name, text, create=False):
        args = {"name": name, "text": text, "create": create}
        return self._run("append_note", args, lambda: Notes.append(self, name, text, create), True)


class RecordingRecipes(Recipes):
    """The recipes folder, logging into the same list as the store. A call to
    add_to_shopping shows up as the add_task calls it makes."""

    def __init__(self, root, calls: list[Call]):
        super().__init__(root)
        self.calls = calls

    def _run(self, tool: str, args: dict[str, Any], fn, write: bool = False):
        call = Call(tool, args)
        self.calls.append(call)
        try:
            out = fn()
        except Exception:
            call.ok = False
            raise
        call.changed = write
        return out

    def list(self, query=None):
        return self._run("list_recipes", {"query": query}, lambda: Recipes.list(self, query))

    def read(self, name):
        return self._run("read_recipe", {"name": name}, lambda: Recipes.read(self, name))

    def guide(self):
        return self._run("read_cooking_guide", {}, lambda: Recipes.guide(self))

    def add_note(self, name, text):
        args = {"name": name, "text": text}
        return self._run("add_recipe_note", args, lambda: Recipes.add_note(self, name, text), True)

    def change_text(self, name, old, new):
        args = {"name": name, "old_text": old, "new_text": new}
        fn = lambda: Recipes.change_text(self, name, old, new)  # noqa: E731
        return self._run("change_recipe_text", args, fn, True)

    def set_tag(self, name, tag, value):
        args = {"name": name, "tag": tag, "value": value}
        return self._run(
            "set_recipe_tag", args, lambda: Recipes.set_tag(self, name, tag, value), True
        )

    def save_plan(self, title, text):
        args = {"title": title, "text": text}
        return self._run("save_meal_plan", args, lambda: Recipes.save_plan(self, title, text), True)

    def shopping(self, name):
        return self._run("add_to_shopping", {"name": name}, lambda: Recipes.shopping(self, name))


def seeded_store(seed: dict[str, Any], clock=None) -> RecordingStore:
    """Build a store from a resolved seed: {tasks: {list: [..]}, events: [..],
    jobs: [..]}. A task is a title or {title, due, completed}; uids are opaque on
    purpose so a model has to read them from list_tasks. A job is a repeating
    task: {list, title, rank, every ('7d' or '1h'), due, after, resets}, the
    last two naming another job by title. Events that name the same `series`
    are days of one repeating event and share a uid."""
    store = RecordingStore()
    if clock is not None:  # a run pinned to a day that is not today
        store.clock = clock
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
    series: dict[str, str] = {}
    for ev in seed.get("events") or []:
        uid = str(uuid.uuid4())
        if ev.get("series"):
            uid = series.setdefault(ev["series"], uid)
        store.seeded_titles[uid] = ev["title"]
        store.events.append(
            Event(
                uid=uid,
                recurring=bool(ev.get("series")),
                title=ev["title"],
                start=datetime.fromisoformat(ev["start"]),
                end=datetime.fromisoformat(ev["end"]) if ev.get("end") else None,
                notes=ev.get("notes"),
                calendar=ev.get("calendar", "personal"),
            )
        )
    by_title: dict[str, str] = {}
    for j in seed.get("jobs") or []:
        n, unit = re.fullmatch(r"(\d+)([dh])", j["every"]).groups()
        job = store.add_job(
            j["list"],
            j["title"],
            timedelta(days=int(n)) if unit == "d" else timedelta(hours=int(n)),
            rank=j["rank"],
            due=datetime.fromisoformat(j["due"]) if j.get("due") else None,
            after=by_title.get(j.get("after", "")),
            resets=by_title.get(j.get("resets", "")),
        )
        by_title[j["title"]] = job.uid
    return store


class EvalServer:
    """One uvicorn for the whole run; each case swaps in a fresh store."""

    def __init__(self, clock=None) -> None:
        self.clock = clock
        self.store = RecordingStore()
        self.notes_dir: str | None = None
        self.recipes_dir: str | None = None
        self.url = ""
        self._server: uvicorn.Server | None = None

    def __enter__(self) -> EvalServer:
        # The loopback instance is open: no token, nothing but this run can reach it.
        os.environ.pop("PIM_TOOLS_TOKEN", None)
        app.dependency_overrides[get_store] = lambda: self.store
        app.dependency_overrides[get_notes] = lambda: self.store.notes
        app.dependency_overrides[get_recipes] = lambda: self.store.recipes
        self.reset({})
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
        app.dependency_overrides.pop(get_notes, None)
        app.dependency_overrides.pop(get_recipes, None)
        for d in (self.notes_dir, self.recipes_dir):
            if d:
                shutil.rmtree(d, ignore_errors=True)

    def reset(self, seed: dict[str, Any]) -> RecordingStore:
        store = seeded_store(seed, self.clock)
        if self.notes_dir:
            shutil.rmtree(self.notes_dir, ignore_errors=True)
        self.notes_dir = tempfile.mkdtemp(prefix="pim-eval-notes-")
        for name, text in (seed.get("notes") or {}).items():
            path = Path(self.notes_dir, name + ".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        store.notes = RecordingNotes(self.notes_dir, store.calls)
        if self.recipes_dir:
            shutil.rmtree(self.recipes_dir, ignore_errors=True)
        self.recipes_dir = tempfile.mkdtemp(prefix="pim-eval-recipes-")
        for name, text in (seed.get("recipes") or {}).items():
            path = Path(self.recipes_dir, name + ".md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        store.recipes = RecordingRecipes(self.recipes_dir, store.calls)
        self.store = store
        return self.store
