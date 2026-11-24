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

    def is_due(self, now: datetime) -> bool:
        return self.due <= now


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
        job.due = WAITING if job.after else done + job.every
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
    if job.after:
        before = next((j.title for j in jobs if j.uid == job.after), "the job before it")
        return f"{span} after '{before}'"
    return f"every {span}"


def _span(d: timedelta) -> str:
    hours = d.total_seconds() / 3600
    if hours < 24 or hours % 24:
        n = round(hours)
        return f"{n} hour" + ("" if n == 1 else "s")
    n = d.days
    return f"{n} day" + ("" if n == 1 else "s")
