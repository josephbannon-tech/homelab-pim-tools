"""Repeating jobs: tasks that come back a set time after they were last done.

A calendar RRULE repeats on fixed dates ("every second Monday"). A household
job is different: the bathroom is due a week after it was last cleaned,
whenever that was. So the interval lives on the task and the next due time is
worked out here, when the task is ticked.

A job has a base rank, its place in one agreed order. Jobs that are due are
shown in that order, except that a late job climbs: its rank shrinks in
proportion to how late it is against its own interval, reaching the top when
it is a whole interval late. A weekly job a week late and a quarterly job
three months late have both been skipped once, and both are first.

A job can also be fixed to the calendar (`on`, an RRULE such as
FREQ=YEARLY;BYMONTH=8;BYMONTHDAY=13). For those `every` is the notice: the job
appears that long before the date, climbs over that notice, and reaches the
top on the day itself. Ticking it schedules the next date from the rule, not
from when it was done, so a birthday ticked late does not drift.

Two links between jobs:
  after   the job waits, hidden, until the named job is done, then comes due
          one interval later (wash, then hang out an hour after)
  resets  doing this job also restarts the named job's interval (a deep clean
          counts as the weekly one)

Everything here is pure: jobs in, jobs out, the clock passed in.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

# Where a chained job sits while it waits for the job before it.
WAITING = datetime(2099, 1, 1, tzinfo=UTC)


@dataclass
class Job:
    uid: str
    title: str
    rank: int
    every: timedelta
    due: datetime
    after: str | None = None
    resets: str | None = None
    done_at: datetime | None = None  # ticked, not yet rescheduled
    position: int | None = None  # the stored display order
    on: str | None = None  # an RRULE: the job is fixed to these dates
    zone: str = "Europe/London"  # whose midnight a fixed date starts at

    def is_due(self, now: datetime) -> bool:
        return self.due <= now


def _dates(job: Job):
    """The job's fixed dates, each a local midnight."""
    start = datetime(2000, 1, 1, tzinfo=ZoneInfo(job.zone))
    return rrulestr(job.on, dtstart=start)


def first_due(job: Job, now: datetime) -> datetime:
    """When a newly loaded fixed-date job should first appear: the notice
    before its next date. Today's date still counts, so a job loaded on the
    day is due at once."""
    day = _dates(job).after(now - timedelta(days=1), inc=False)
    return day - job.every


def next_due(job: Job, done: datetime) -> datetime:
    """After a fixed-date job is ticked: the notice before the first later
    date whose notice has not already begun when it was done."""
    rule = _dates(job)
    day = rule.after(job.due)  # the date this turn was for
    while True:
        day = rule.after(day)
        if day - job.every > done:
            return day - job.every


def effective_rank(job: Job, now: datetime) -> float:
    late = (now - job.due) / job.every
    return job.rank * (1.0 - min(max(late, 0.0), 1.0))


def in_order(jobs: list[Job], now: datetime) -> list[Job]:
    """The jobs that are due, first one first."""
    due = [j for j in jobs if j.is_due(now)]
    return sorted(due, key=lambda j: (effective_rank(j, now), j.rank))


def settle(jobs: list[Job], now: datetime) -> set[str]:
    """Reschedule every ticked job, follow its links, and bring the stored
    display order into line. Returns the uids whose stored form changed."""
    by_uid = {j.uid: j for j in jobs}
    changed: set[str] = set()
    ticked = sorted((j for j in jobs if j.done_at), key=lambda j: j.done_at)
    for job in ticked:
        done, job.done_at = job.done_at, None
        if job.on:
            job.due = next_due(job, done)
        elif job.after:
            job.due = WAITING
        else:
            job.due = done + job.every
        changed.add(job.uid)
        for nxt in jobs:
            if nxt.after == job.uid:
                nxt.due = done + nxt.every
                changed.add(nxt.uid)
        target = by_uid.get(job.resets or "")
        if target is not None and not target.after:
            target.due = done + target.every
            changed.add(target.uid)

    # Display order. Only the due jobs are visible, so only their relative
    # order matters; rewrite positions when that order is wrong, not on every
    # pass, or each sweep would touch every job and every phone would resync.
    wanted = in_order(jobs, now)
    stored = [j.position for j in wanted]
    if None in stored or any(a >= b for a, b in itertools.pairwise(stored)):
        for pos, job in enumerate(wanted, 1):
            if job.position != pos:
                job.position = pos
                changed.add(job.uid)
    return changed


def describe(job: Job, jobs: list[Job]) -> str:
    """How the job repeats, in words a reply can quote."""
    span = _span(job.every)
    if job.on:
        return f"{_when(job.on)}, shown {span} before"
    if job.after:
        before = next((j.title for j in jobs if j.uid == job.after), "the job before it")
        return f"{span} after '{before}'"
    return f"every {span}"


_MONTHS = "January February March April May June July August September October November December"


def _when(on: str) -> str:
    parts = dict(p.split("=", 1) for p in on.split(";") if "=" in p)
    if parts.get("FREQ") == "YEARLY" and "BYMONTH" in parts and "BYMONTHDAY" in parts:
        month = _MONTHS.split()[int(parts["BYMONTH"]) - 1]
        return f"every year on {int(parts['BYMONTHDAY'])} {month}"
    return f"on fixed dates ({on})"


def _span(d: timedelta) -> str:
    hours = d.total_seconds() / 3600
    if hours < 24 or hours % 24:
        n = round(hours)
        return f"{n} hour" + ("" if n == 1 else "s")
    n = d.days
    return f"{n} day" + ("" if n == 1 else "s")
