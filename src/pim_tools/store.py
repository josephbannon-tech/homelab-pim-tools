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

from .models import CalendarInfo, Event, Task
from .repeat import Job, describe, in_order, settle


class NotFound(Exception):
    pass


class Unsupported(Exception):
    """A change this service will not make (an all-day event, for one)."""


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
        self,
        title: str,
        start: datetime,
        end: datetime | None,
        notes: str | None,
        calendar: str | None = None,
    ) -> tuple[Event, bool]: ...
    def calendars_info(self) -> list[CalendarInfo]: ...
    def move_event(
        self, calendar: str, uid: str, start: datetime, to_calendar: str, whole_series: bool
    ) -> tuple[Event, str]: ...
    def change_event(
        self,
        calendar: str,
        uid: str,
        start: datetime,
        new_start: datetime | None,
        new_end: datetime | None,
        new_title: str | None,
        notes: str | None,
    ) -> tuple[Event, bool]: ...
    def cancel_event(self, calendar: str, uid: str, start: datetime) -> Event: ...


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _unescape(text: str) -> str:
    """Undo TEXT escaping on an X- property. Radicale rewrites what it stores
    and escapes the semicolons of an RRULE held in X-CHORE-ON (FREQ=YEARLY\\;
    BYMONTH=8), and icalendar hands an unknown property back as written."""
    return text.replace("\\;", ";").replace("\\,", ",").replace("\\\\", "\\")


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


def _changed_times(
    start: datetime, end: datetime, new_start: datetime | None, new_end: datetime | None
) -> tuple[datetime, datetime]:
    """A new start keeps the length unless a new end is given too."""
    length = end - start
    if new_start is not None:
        start = _aware(new_start)
        end = start + length
    if new_end is not None:
        end = _aware(new_end)
    if end <= start:
        raise Unsupported("the end must be after the start")
    return start, end


def _utc(dt: datetime) -> datetime:
    """Normalise to UTC before anything is serialised. A fixed-offset tzinfo
    (what an ISO string like 2026-10-22T10:00:00+01:00 parses to) would otherwise
    be written as DTSTART;TZID="+01:00", which names no VTIMEZONE and leaves each
    client to guess. UTC serialises as a plain ...Z stamp that every client reads."""
    return _aware(dt).astimezone(UTC)


def _wanted(
    calendar: str | None, known: tuple[str, ...], background: tuple[str, ...] = ("routine",)
) -> tuple[str, ...]:
    """Which calendars a read covers. None is every appointment calendar (all
    but the background ones, the daily routine); 'all' is every configured one."""
    if calendar is None:
        return tuple(c for c in known if c not in background) or known[:1]
    if calendar == "all":
        return known
    if calendar not in known:
        raise NotFound(calendar)
    return (calendar,)


def _check_move(
    calendar: str,
    to_calendar: str,
    known: tuple[str, ...],
    background: tuple[str, ...],
    series: bool,
) -> None:
    """A move goes between configured calendars. A whole routine series stays
    put (the routine keeps one colour by design); one day of it may move."""
    if to_calendar not in known:
        raise NotFound(to_calendar)
    if to_calendar == calendar:
        raise Unsupported(f"the event is already on {calendar}")
    if to_calendar in background:
        raise Unsupported(f"events are not moved onto {to_calendar}")
    if series and calendar in background:
        raise Unsupported(f"a whole {calendar} series stays on {calendar}; move one day instead")


# --------------------------------------------------------------------------- memory


@dataclass
class MemoryStore:
    tasks: dict[str, list[Task]] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    calendars: tuple[str, ...] = ("personal", "routine")
    colours: dict[str, str] = field(default_factory=dict)  # calendar -> #rrggbb
    background: tuple[str, ...] = ("routine",)
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
        on: str | None = None,
    ) -> Job:
        """Seed a repeating job (tests and evals; production jobs are loaded
        into the CalDAV server directly). A job with `on` is fixed to those
        dates and `every` is its notice."""
        from .repeat import WAITING, first_due

        job = Job(str(uuid.uuid4()), title, rank, every, self.clock(), after, resets, on=on)
        if due is not None:
            job.due = _aware(due)
        elif on:
            job.due = first_due(job, self.clock())
        elif after:
            job.due = WAITING
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
        wanted = _wanted(calendar, self.calendars, self.background)
        start, end = _aware(start), _aware(end)
        out = [
            e
            for e in self.events
            if e.calendar in wanted and _aware(e.start) < end and _aware(e.end or e.start) >= start
        ]
        return sorted(out, key=lambda e: _aware(e.start))

    def add_event(
        self,
        title: str,
        start: datetime,
        end: datetime | None,
        notes: str | None,
        calendar: str | None = None,
    ) -> tuple[Event, bool]:
        target = calendar or self.calendars[0]
        if target not in self.calendars:
            raise NotFound(target)
        if target in self.background:
            raise Unsupported(f"new events do not go on {target}")
        start = _aware(start)
        end = _aware(end) if end else start + timedelta(hours=1)
        for e in self.events:
            if _norm(e.title) == _norm(title) and _aware(e.start) == start:
                return e, False
        e = Event(
            uid=str(uuid.uuid4()),
            title=title.strip(),
            start=start,
            end=end,
            notes=notes,
            calendar=target,
        )
        self.events.append(e)
        return e, True

    def calendars_info(self) -> list[CalendarInfo]:
        return [
            CalendarInfo(
                name=c,
                colour=self.colours.get(c),
                routine=c in self.background,
                default=i == 0,
            )
            for i, c in enumerate(self.calendars)
        ]

    def move_event(
        self, calendar: str, uid: str, start: datetime, to_calendar: str, whole_series: bool
    ) -> tuple[Event, str]:
        """Each stored Event is one day here; a series is the events sharing a uid."""
        e = self._one(calendar, uid, start)
        series = e.recurring and whole_series
        _check_move(calendar, to_calendar, self.calendars, self.background, series)
        if not e.recurring or series:
            for other in self.events:
                if other.uid == uid and other.calendar == calendar:
                    other.calendar = to_calendar
            return e, "every day of the series" if series else "this single event"
        copy = e.model_copy(
            update={"uid": str(uuid.uuid4()), "calendar": to_calendar, "recurring": False}
        )
        self.events.remove(e)
        self.events.append(copy)
        return copy, f"only {copy.start:%A %d %B}; the rest of the series stays on {calendar}"

    def _one(self, calendar: str, uid: str, start: datetime) -> Event:
        if calendar not in self.calendars:
            raise NotFound(calendar)
        start = _aware(start)
        for e in self.events:
            if e.calendar == calendar and e.uid == uid and _aware(e.start) == start:
                return e
        raise NotFound(uid)

    def change_event(
        self,
        calendar: str,
        uid: str,
        start: datetime,
        new_start: datetime | None,
        new_end: datetime | None,
        new_title: str | None,
        notes: str | None,
    ) -> tuple[Event, bool]:
        """Each stored Event is one day here, so a repeating series is several
        events sharing a uid and changing one never reaches the others."""
        e = self._one(calendar, uid, start)
        want = _changed_times(_aware(e.start), _aware(e.end or e.start), new_start, new_end)
        before = (e.start, e.end, e.title, e.notes)
        e.start, e.end = want
        e.title = new_title.strip() if new_title else e.title
        e.notes = notes if notes is not None else e.notes
        return e, (e.start, e.end, e.title, e.notes) != before

    def cancel_event(self, calendar: str, uid: str, start: datetime) -> Event:
        e = self._one(calendar, uid, start)
        self.events.remove(e)
        return e


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
        background: tuple[str, ...] = ("routine",),
    ):
        import caldav  # imported lazily so the API tests need no CalDAV stack

        self._client = caldav.DAVClient(url=url, username=username, password=password)
        self._principal = self._client.principal()
        self._event_calendar_name = calendar
        self._calendars = (calendar, *extra_calendars)
        self._background = background

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
            # an expanded search marks every day of a series with RECURRENCE-ID
            recurring="RECURRENCE-ID" in ve or "RRULE" in ve,
        )

    # -- tasks
    def task_lists(self) -> list[str]:
        """Every collection that holds tasks and is not a configured event
        calendar, by URL leaf. An event-only collection (a new calendar not yet
        configured here) is not offered as a list."""
        out = []
        for c in self._principal.calendars():
            leaf = str(c.url).rstrip("/").rsplit("/", 1)[-1]
            if leaf in self._calendars:
                continue
            try:
                comps = c.get_supported_components()
            except Exception:  # a server that does not say: keep the old behaviour
                comps = []
            if not comps or "VTODO" in comps:
                out.append(leaf)
        return sorted(out)

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
            on=_unescape(str(vt.get("x-chore-on"))) if vt.get("x-chore-on") else None,
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
        for name in _wanted(calendar, self._calendars, self._background):
            found = self._calendar(name).search(
                start=_aware(start), end=_aware(end), event=True, expand=True
            )
            out += [self._event_from(e, name) for e in found]
        return sorted(out, key=lambda e: e.start)

    def add_event(
        self,
        title: str,
        start: datetime,
        end: datetime | None,
        notes: str | None,
        calendar: str | None = None,
    ) -> tuple[Event, bool]:
        target = calendar or self._event_calendar_name
        if target not in self._calendars:
            raise NotFound(target)
        if target in getattr(self, "_background", ()):
            raise Unsupported(f"new events do not go on {target}")
        cal = self._calendar(target)
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
        return self._event_from(ev, target), True

    def calendars_info(self) -> list[CalendarInfo]:
        from caldav.elements import ical as ical_props

        out = []
        for i, name in enumerate(self._calendars):
            try:
                colour = self._calendar(name).get_property(ical_props.CalendarColor())
            except Exception:
                colour = None
            out.append(
                CalendarInfo(
                    name=name,
                    colour=str(colour)[:7] if colour else None,
                    routine=name in self._background,
                    default=i == 0,
                )
            )
        return out

    # -- changing one event, or one day of a repeating one
    #
    # A repeating event is one resource: a master VEVENT with the RRULE, plus one
    # VEVENT per day that differs (RECURRENCE-ID names the day it replaces) and
    # an EXDATE on the master per day that is skipped. These two methods only
    # ever add or edit those per-day parts. Nothing here can rewrite an RRULE or
    # delete a series, so a wrong call costs one day, which shows on the phone
    # and is put right by hand.

    def _locate(self, calendar: str, uid: str, start: datetime):
        """(event object, its calendar, master, the VEVENT for that day or None,
        that day as the series knows it). Raises NotFound when no event with
        this uid starts then."""
        import recurring_ical_events
        from caldav.lib import error as caldav_error

        if calendar not in self._calendars:
            raise NotFound(calendar)
        try:
            obj = self._calendar(calendar).event_by_uid(uid)
        except caldav_error.NotFoundError as e:
            raise NotFound(uid) from e
        ical = obj.icalendar_instance
        parts = ical.walk("VEVENT")
        master = next((c for c in parts if "RECURRENCE-ID" not in c), None)
        if master is None:
            raise NotFound(uid)
        if not isinstance(master["DTSTART"].dt, datetime):
            raise Unsupported("all-day events cannot be changed here")
        start = _aware(start)
        day = next(
            (
                o
                for o in recurring_ical_events.of(ical).between(
                    start - timedelta(minutes=1), start + timedelta(minutes=1)
                )
                if _aware(o["DTSTART"].dt) == start
            ),
            None,
        )
        if day is None:
            raise NotFound(uid)
        if "RRULE" not in master:
            return obj, ical, master, master, None
        # The day in the series' own terms: for a day already moved, the slot it
        # was moved from; otherwise the start itself, in the master's timezone.
        rid = day["RECURRENCE-ID"].dt if "RECURRENCE-ID" in day else day["DTSTART"].dt
        zone = master["DTSTART"].dt.tzinfo
        rid = _aware(rid).astimezone(zone) if zone else _aware(rid)
        override = next(
            (c for c in parts if "RECURRENCE-ID" in c and _aware(c["RECURRENCE-ID"].dt) == rid),
            None,
        )
        return obj, ical, master, override, rid

    @staticmethod
    def _as_event(ve, calendar: str, recurring: bool) -> Event:
        start = _aware(ve["DTSTART"].dt)
        end = _aware(ve["DTEND"].dt) if "DTEND" in ve else None
        if end is None and "DURATION" in ve:
            end = start + ve["DURATION"].dt
        desc = ve.get("description")
        return Event(
            uid=str(ve.get("uid")),
            title=str(ve.get("summary", "")),
            start=start,
            end=end,
            notes=str(desc) if desc else None,
            calendar=calendar,
            recurring=recurring,
        )

    def _day_view(self, master, target, rid, calendar: str) -> Event:
        """The event as it shows on that day."""
        if target is not None:
            return self._as_event(target, calendar, rid is not None)
        length = self._as_event(master, calendar, True)
        span = (length.end - length.start) if length.end else timedelta(hours=1)
        return length.model_copy(update={"start": _aware(rid), "end": _aware(rid) + span})

    def move_event(
        self, calendar: str, uid: str, start: datetime, to_calendar: str, whole_series: bool
    ) -> tuple[Event, str]:
        """Move an event to another calendar (which sets its colour on the phone).
        A single event or a whole series moves as one resource, keeping its uid:
        written to the new calendar first, then deleted from the old, so a
        failure in between leaves a duplicate, never a loss. One day of a series
        becomes a single event on the new calendar and is skipped in the series."""
        from caldav.lib import error as caldav_error

        obj, ical, master, target, rid = self._locate(calendar, uid, start)
        series = rid is not None and whole_series
        _check_move(calendar, to_calendar, self._calendars, self._background, series)
        dest = self._calendar(to_calendar)
        day = self._day_view(master, target, rid, calendar)
        if rid is None or series:
            try:
                dest.event_by_uid(uid)
            except caldav_error.NotFoundError:
                pass
            else:
                raise Unsupported(f"{to_calendar} already holds an event with this uid")
            dest.save_event(ical.to_ical().decode())
            obj.delete()
            moved = day.model_copy(update={"calendar": to_calendar})
            return moved, "every day of the series" if series else "this single event"
        kwargs = {
            "summary": day.title,
            "dtstart": _utc(day.start),
            "dtend": _utc(day.end or day.start + timedelta(hours=1)),
            "uid": str(uuid.uuid4()),
        }
        if day.notes:
            kwargs["description"] = day.notes
        copy = dest.save_event(**kwargs)
        try:
            self.cancel_event(calendar, uid, start)
        except Exception:
            copy.delete()
            raise
        return (
            self._event_from(copy, to_calendar),
            f"only {day.start:%A %d %B}; the rest of the series stays on {calendar}",
        )

    def change_event(
        self,
        calendar: str,
        uid: str,
        start: datetime,
        new_start: datetime | None,
        new_end: datetime | None,
        new_title: str | None,
        notes: str | None,
    ) -> tuple[Event, bool]:
        from icalendar import Event as VEvent

        obj, ical, master, target, rid = self._locate(calendar, uid, start)
        recurring = rid is not None
        zone = master["DTSTART"].dt.tzinfo or UTC
        if target is not None:
            now = self._as_event(target, calendar, recurring)
        else:  # an untouched day of a series: what the master says, on that day
            length = self._as_event(master, calendar, True)
            span = (length.end - length.start) if length.end else timedelta(hours=1)
            now = length.model_copy(update={"start": _aware(rid), "end": _aware(rid) + span})
        want_start, want_end = _changed_times(now.start, now.end or now.start, new_start, new_end)
        want_title = new_title.strip() if new_title else now.title
        want_notes = notes if notes is not None else now.notes
        if (want_start, want_end, want_title, want_notes) == (
            now.start,
            now.end,
            now.title,
            now.notes,
        ):
            return now, False
        if target is None:
            target = VEvent()
            target.add("uid", uid)
            target.add("recurrence-id", rid)
            for keep in ("location", "categories", "transp", "class"):
                if keep in master:
                    target.add(keep, master[keep])
            ical.add_component(target)
        for prop in ("dtstart", "dtend", "duration", "summary", "description", "dtstamp"):
            target.pop(prop, None)
        target.add("dtstamp", _now())
        target.add("dtstart", want_start.astimezone(zone))
        target.add("dtend", want_end.astimezone(zone))
        target.add("summary", want_title)
        if want_notes:
            target.add("description", want_notes)
        obj.icalendar_instance = ical
        obj.save()
        return self._as_event(target, calendar, recurring), True

    def cancel_event(self, calendar: str, uid: str, start: datetime) -> Event:
        obj, ical, master, target, rid = self._locate(calendar, uid, start)
        if rid is None:  # a single event: the whole resource goes
            gone = self._as_event(master, calendar, False)
            obj.delete()
            return gone
        if target is not None:
            gone = self._as_event(target, calendar, True)
            ical.subcomponents.remove(target)
        else:
            was = self._as_event(master, calendar, True)
            span = (was.end - was.start) if was.end else timedelta(hours=1)
            gone = was.model_copy(update={"start": _aware(rid), "end": _aware(rid) + span})
        master.add("exdate", rid)
        obj.icalendar_instance = ical
        obj.save()
        return gone
