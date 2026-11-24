"""Store backends.

`CalDAVStore` talks to Radicale over CalDAV (the production path). `MemoryStore`
is the in-process double used by the API tests; both implement the same
protocol so the HTTP layer is tested without a server.

Idempotency lives here, not in the prompt: adding a task whose title matches an
open task on the same list (case-insensitive, same due date) returns the
existing task with created=False; the same for an event with equal title and
start. A retrying LLM cannot create duplicates.

A task list can hold repeating jobs (see repeat.py). For those, list_tasks
returns the ones due now in the order to do them, and complete_task
reschedules the job instead of closing it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from .models import Event, Task
from .repeat import Job, describe, in_order, settle


class NotFound(Exception):
    pass


class Store(Protocol):
    def task_lists(self) -> list[str]: ...
    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]: ...
    def add_task(self, list_name: str, title: str, due: date | None) -> tuple[Task, bool]: ...
    def complete_task(self, list_name: str, uid: str) -> Task: ...
    def sweep(self, list_name: str) -> int: ...
    def list_events(
        self, start: datetime, end: datetime, calendar: str | None = None
    ) -> list[Event]: ...
    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]: ...


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _job_task(list_name: str, job: Job, jobs: list[Job], done: bool) -> Task:
    return Task(
        uid=job.uid, list=list_name, title=job.title, completed=done, repeats=describe(job, jobs)
    )


def _job_view(list_name: str, jobs: list[Job], now: datetime, include_done: bool) -> list[Task]:
    """Due jobs, first one first. With include_done, the jobs that are not due
    follow as completed: done for now is what 'completed' means for a job."""
    out = [_job_task(list_name, j, jobs, False) for j in in_order(jobs, now)]
    if include_done:
        rest = sorted((j for j in jobs if not j.is_due(now)), key=lambda j: (j.due, j.rank))
        out += [_job_task(list_name, j, jobs, True) for j in rest]
    return out


def _utc(dt: datetime) -> datetime:
    """Normalise to UTC before anything is serialised. A fixed-offset tzinfo
    (what an ISO string like 2026-10-22T10:00:00+01:00 parses to) would otherwise
    be written as DTSTART;TZID="+01:00", which names no VTIMEZONE and leaves each
    client to guess. UTC serialises as a plain ...Z stamp that every client reads."""
    return _aware(dt).astimezone(UTC)


def _wanted(calendar: str | None, known: tuple[str, ...]) -> tuple[str, ...]:
    """Which calendars a read covers. None is the first (the appointments
    calendar, also the only one written to); 'all' is every configured one."""
    if calendar is None:
        return known[:1]
    if calendar == "all":
        return known
    if calendar not in known:
        raise NotFound(calendar)
    return (calendar,)


# --------------------------------------------------------------------------- memory


@dataclass
class MemoryStore:
    tasks: dict[str, list[Task]] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    calendars: tuple[str, ...] = ("personal", "routine")
    jobs: dict[str, Job] = field(default_factory=dict)  # repeating jobs, by task uid
    clock: Callable[[], datetime] = _now

    def task_lists(self) -> list[str]:
        return sorted(self.tasks)

    def add_job(
        self,
        list_name: str,
        title: str,
        every: timedelta,
        rank: int,
        due: datetime | None = None,
        after: str | None = None,
        resets: str | None = None,
    ) -> Job:
        """Seed a repeating job (tests and evals; production jobs are loaded
        into the CalDAV server directly)."""
        from .repeat import WAITING

        start = due or (WAITING if after else self.clock())
        job = Job(str(uuid.uuid4()), title, rank, every, _aware(start), after, resets)
        self.tasks.setdefault(list_name, []).append(Task(uid=job.uid, list=list_name, title=title))
        self.jobs[job.uid] = job
        return job

    def _jobs(self, list_name: str) -> list[Job]:
        return [self.jobs[t.uid] for t in self.tasks.get(list_name, []) if t.uid in self.jobs]

    def sweep(self, list_name: str) -> int:
        return len(settle(self._jobs(list_name), self.clock()))

    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]:
        items = self.tasks.get(list_name)
        if items is None:
            raise NotFound(list_name)
        jobs = self._jobs(list_name)
        settle(jobs, self.clock())
        plain = [t for t in items if t.uid not in self.jobs]
        return _job_view(list_name, jobs, self.clock(), include_completed) + [
            t for t in plain if include_completed or not t.completed
        ]

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
                if uid in self.jobs:
                    jobs, now = self._jobs(list_name), self.clock()
                    job = self.jobs[uid]
                    if job.is_due(now):  # ticking a job that is not due changes nothing
                        job.done_at = now
                        settle(jobs, now)
                    return _job_task(list_name, job, jobs, True)
                t.completed = True
                return t
        raise NotFound(uid)

    def list_events(
        self, start: datetime, end: datetime, calendar: str | None = None
    ) -> list[Event]:
        wanted = _wanted(calendar, self.calendars)
        start, end = _aware(start), _aware(end)
        out = [
            e
            for e in self.events
            if e.calendar in wanted and _aware(e.start) < end and _aware(e.end or e.start) >= start
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

_STAMP = "%Y%m%dT%H%M%SZ"


class CalDAVStore:
    """Radicale over CalDAV. One principal, calendars addressed by their URL leaf
    (`/jbannon/shopping/` -> 'shopping'), which is what the phone apps show."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar: str = "personal",
        extra_calendars: tuple[str, ...] = (),
    ):
        import caldav  # imported lazily so the API tests need no CalDAV stack

        self._client = caldav.DAVClient(url=url, username=username, password=password)
        self._principal = self._client.principal()
        self._event_calendar_name = calendar
        self._calendars = (calendar, *extra_calendars)

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
    def _event_from(ev, calendar: str = "personal") -> Event:
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
            calendar=calendar,
        )

    # -- tasks
    def task_lists(self) -> list[str]:
        """Every collection that is not an event calendar, by URL leaf."""
        leaves = (str(c.url).rstrip("/").rsplit("/", 1)[-1] for c in self._principal.calendars())
        return sorted(leaf for leaf in leaves if leaf not in self._calendars)

    # -- repeating jobs: the interval and rank ride on the VTODO as X- properties
    @staticmethod
    def _job_from(todo, now: datetime) -> Job | None:
        from icalendar import vDuration

        vt = todo.icalendar_component
        every = vt.get("x-chore-every")
        if every is None:
            return None
        due = vt.get("x-chore-due")
        done_at = None
        if str(vt.get("status", "")).upper() == "COMPLETED":
            stamp = vt.get("completed")
            done_at = _aware(stamp.dt) if stamp is not None else now
        pos = vt.get("x-apple-sort-order")
        return Job(
            uid=str(vt.get("uid")),
            title=str(vt.get("summary", "")),
            rank=int(str(vt.get("x-chore-rank", "999"))),
            every=vDuration.from_ical(str(every)),
            due=datetime.strptime(str(due), _STAMP).replace(tzinfo=UTC) if due else now,
            after=str(vt.get("x-chore-after")) if vt.get("x-chore-after") else None,
            resets=str(vt.get("x-chore-resets")) if vt.get("x-chore-resets") else None,
            done_at=done_at,
            position=int(str(pos)) if pos is not None and str(pos).isdigit() else None,
        )

    @staticmethod
    def _write_job(todo, job: Job, now: datetime) -> None:
        """Store a job as open, due at job.due. A future DTSTART is what task
        apps read as 'hide until'; X-APPLE-SORT-ORDER is their manual order."""
        from caldav.lib import error as caldav_error

        vt = todo.icalendar_component
        for gone in (
            "completed",
            "percent-complete",
            "dtstart",
            "x-chore-due",
            "x-apple-sort-order",
        ):
            vt.pop(gone, None)
        vt["status"] = "NEEDS-ACTION"
        vt.add("x-chore-due", job.due.strftime(_STAMP))
        if job.due > now:
            vt.add("dtstart", job.due)
        if job.position is not None:
            vt.add("x-apple-sort-order", str(job.position))
        try:
            todo.save()
        except caldav_error.ETagMismatchError:
            # Edited on a phone between our read and this write. Theirs stands;
            # the next pass reads it fresh and settles it then.
            pass

    def _settled(self, list_name: str, now: datetime) -> tuple[list[Job], list, int]:
        """(jobs, the other todos, how many jobs were rewritten)."""
        todos = self._calendar(list_name).todos(include_completed=True)
        pairs = [(t, self._job_from(t, now)) for t in todos]
        jobs = [j for _, j in pairs if j is not None]
        changed = settle(jobs, now)
        for todo, job in pairs:
            if job is not None and job.uid in changed:
                self._write_job(todo, job, now)
        return jobs, [t for t, j in pairs if j is None], len(changed)

    def sweep(self, list_name: str) -> int:
        return self._settled(list_name, _now())[2]

    def list_tasks(self, list_name: str, include_completed: bool = False) -> list[Task]:
        now = _now()
        jobs, plain, _ = self._settled(list_name, now)
        tasks = [self._task_from(list_name, t) for t in plain]
        return _job_view(list_name, jobs, now, include_completed) + [
            t for t in tasks if include_completed or not t.completed
        ]

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
        now = _now()
        for todo in cal.todos(include_completed=True):
            if str(todo.icalendar_component.get("uid")) != uid:
                continue
            job = self._job_from(todo, now)
            if job is None:
                if str(todo.icalendar_component.get("status", "")).upper() == "COMPLETED":
                    break
                todo.complete()
                todo.load()
                return self._task_from(list_name, todo)
            if job.done_at is None and job.is_due(now):
                todo.icalendar_component["status"] = "COMPLETED"
                todo.icalendar_component.pop("completed", None)
                todo.icalendar_component.add("completed", now)
                todo.save()
            jobs, _, _ = self._settled(list_name, now)
            mine = next(j for j in jobs if j.uid == uid)
            return _job_task(list_name, mine, jobs, True)
        raise NotFound(uid)

    # -- events
    def list_events(
        self, start: datetime, end: datetime, calendar: str | None = None
    ) -> list[Event]:
        out: list[Event] = []
        for name in _wanted(calendar, self._calendars):
            found = self._calendar(name).search(
                start=_aware(start), end=_aware(end), event=True, expand=True
            )
            out += [self._event_from(e, name) for e in found]
        return sorted(out, key=lambda e: e.start)

    def add_event(
        self, title: str, start: datetime, end: datetime | None, notes: str | None
    ) -> tuple[Event, bool]:
        cal = self._calendar(self._event_calendar_name)
        start = _utc(start)
        end = _utc(end) if end else start + timedelta(hours=1)
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
        return self._event_from(ev, self._event_calendar_name), True
