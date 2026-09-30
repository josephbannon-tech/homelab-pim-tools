"""Store backends.

`CalDAVStore` talks to Radicale over CalDAV (the production path). `MemoryStore`
is the in-process double used by the API tests; both implement the same
protocol so the HTTP layer is tested without a server.

Idempotency lives here, not in the prompt: adding a task whose title matches an
open task on the same list (case-insensitive, same due date) returns the
existing task with created=False; the same for an event with equal title and
start. A retrying LLM cannot create duplicates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .models import Event, Task


class NotFound(Exception):
    pass


class Store(Protocol):
    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]: ...
    def add_task(self, list_name: str, title: str, due: date | None) -> tuple[Task, bool]: ...
    def complete_task(self, list_name: str, uid: str) -> Task: ...
    def list_events(self, start: datetime, end: datetime) -> list[Event]: ...
    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]: ...


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- memory


@dataclass
class MemoryStore:
    tasks: dict[str, list[Task]] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)

    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]:
        items = self.tasks.get(list_name)
        if items is None:
            raise NotFound(list_name)
        return [t for t in items if include_completed or not t.completed]

    def add_task(self, list_name: str, title: str, due: date | None) -> tuple[Task, bool]:
        items = self.tasks.setdefault(list_name, [])
        for t in items:
            if not t.completed and _norm(t.title) == _norm(title) and t.due == due:
                return t, False
        t = Task(uid=str(uuid.uuid4()), list=list_name, title=title.strip(), due=due)
        items.append(t)
        return t, True

    def complete_task(self, list_name: str, uid: str) -> Task:
        for t in self.tasks.get(list_name, []):
            if t.uid == uid:
                t.completed = True
                return t
        raise NotFound(uid)

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        start, end = _aware(start), _aware(end)
        out = [
            e for e in self.events if _aware(e.start) < end and _aware(e.end or e.start) >= start
        ]
        return sorted(out, key=lambda e: _aware(e.start))

    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]:
        start = _aware(start)
        end = _aware(end) if end else start + timedelta(hours=1)
        for e in self.events:
            if _norm(e.title) == _norm(title) and _aware(e.start) == start:
                return e, False
        e = Event(uid=str(uuid.uuid4()), title=title.strip(), start=start, end=end, notes=notes)
        self.events.append(e)
        return e, True


# --------------------------------------------------------------------------- caldav


class CalDAVStore:
    """Radicale over CalDAV. One principal, calendars addressed by their URL leaf
    (`/jbannon/shopping/` -> 'shopping'), which is what the phone apps show."""

    def __init__(self, url: str, username: str, password: str, calendar: str = "personal"):
        import caldav  # imported lazily so the API tests need no CalDAV stack

        self._client = caldav.DAVClient(url=url, username=username, password=password)
        self._principal = self._client.principal()
        self._event_calendar_name = calendar

    # -- helpers
    def _calendar(self, name: str):
        for cal in self._principal.calendars():
            leaf = str(cal.url).rstrip("/").rsplit("/", 1)[-1]
            if leaf == name:
                return cal
        raise NotFound(name)

    @staticmethod
    def _task_from(list_name: str, todo) -> Task:
        vt = todo.icalendar_component
        due = vt.get("due")
        due_d = due.dt if due is not None else None
        if isinstance(due_d, datetime):
            due_d = due_d.date()
        return Task(
            uid=str(vt.get("uid")),
            list=list_name,
            title=str(vt.get("summary", "")),
            due=due_d,
            completed=str(vt.get("status", "")).upper() == "COMPLETED",
        )

    @staticmethod
    def _event_from(ev) -> Event:
        ve = ev.icalendar_component
        start = ve.get("dtstart").dt
        end = ve.get("dtend").dt if ve.get("dtend") is not None else None
        if not isinstance(start, datetime):
            start = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        if end is not None and not isinstance(end, datetime):
            end = datetime.combine(end, datetime.min.time(), tzinfo=UTC)
        desc = ve.get("description")
        return Event(
            uid=str(ve.get("uid")),
            title=str(ve.get("summary", "")),
            start=_aware(start),
            end=_aware(end) if end else None,
            notes=str(desc) if desc else None,
        )

    # -- tasks
    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]:
        cal = self._calendar(list_name)
        todos = cal.todos(include_completed=include_completed)
        return [self._task_from(list_name, t) for t in todos]

    def add_task(self, list_name: str, title: str, due: date | None) -> tuple[Task, bool]:
        cal = self._calendar(list_name)
        for t in self.list_tasks(list_name):
            if _norm(t.title) == _norm(title) and t.due == due:
                return t, False
        kwargs = {"summary": title.strip(), "uid": str(uuid.uuid4())}
        if due:
            kwargs["due"] = due
        todo = cal.save_todo(**kwargs)
        return self._task_from(list_name, todo), True

    def complete_task(self, list_name: str, uid: str) -> Task:
        cal = self._calendar(list_name)
        for todo in cal.todos(include_completed=False):
            if str(todo.icalendar_component.get("uid")) == uid:
                todo.complete()
                todo.load()
                return self._task_from(list_name, todo)
        raise NotFound(uid)

    # -- events
    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        cal = self._calendar(self._event_calendar_name)
        found = cal.search(start=_aware(start), end=_aware(end), event=True, expand=True)
        return sorted((self._event_from(e) for e in found), key=lambda e: e.start)

    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]:
        cal = self._calendar(self._event_calendar_name)
        start = _aware(start)
        end = _aware(end) if end else start + timedelta(hours=1)
        for e in self.list_events(start - timedelta(minutes=1), start + timedelta(minutes=1)):
            if _norm(e.title) == _norm(title) and e.start == start:
                return e, False
        kwargs = {
            "summary": title.strip(),
            "dtstart": start,
            "dtend": end,
            "uid": str(uuid.uuid4()),
        }
        if notes:
            kwargs["description"] = notes
        ev = cal.save_event(**kwargs)
        return self._event_from(ev), True
