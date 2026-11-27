"""Fixed-date jobs: a birthday appears a set notice before its date, climbs
over that notice to the top on the day, and ticking it schedules next year's
date from the rule, never from when it was ticked."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
from pim_tools.repeat import Job, describe, effective_rank, first_due, in_order, settle
from pim_tools.store import CalDAVStore, MemoryStore

LONDON = ZoneInfo("Europe/London")
DAY = timedelta(days=1)
JAMES = "FREQ=YEARLY;BYMONTH=8;BYMONTHDAY=13"


def local(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=LONDON)


def birthday(on=JAMES, rank=20, due=None):
    title = "Card/present: James W (13 Aug)"
    return Job("james", title, rank, 7 * DAY, due or local(2027, 8, 6), on=on)


def test_it_first_appears_a_week_before_at_local_midnight():
    job = birthday()
    assert first_due(job, datetime(2026, 11, 27, 9, tzinfo=UTC)) == local(2027, 8, 6)


def test_loaded_on_the_day_it_is_due_at_once():
    job = birthday()
    due = first_due(job, local(2026, 8, 13, 15))
    assert due == local(2026, 8, 6)
    assert effective_rank(Job(**{**job.__dict__, "due": due}), local(2026, 8, 13, 15)) == 0


def test_it_climbs_over_its_notice_and_tops_the_list_on_the_day():
    job = birthday()
    assert effective_rank(job, local(2027, 8, 6)) == 20
    assert effective_rank(job, local(2027, 8, 9)) == pytest.approx(20 * 4 / 7)
    assert effective_rank(job, local(2027, 8, 12)) == pytest.approx(20 / 7)
    assert effective_rank(job, local(2027, 8, 13)) == 0
    assert effective_rank(job, local(2027, 8, 20)) == 0  # unticked, it stays on top


def test_it_overtakes_the_daily_jobs_only_near_the_day():
    job = birthday()
    for day, first in [(6, "dishes"), (12, "dishes"), (13, "james")]:
        dishes = Job("dishes", "dishes", 1, DAY, local(2027, 8, day, 8))  # done daily
        assert in_order([dishes, job], local(2027, 8, day, 9))[0].uid == first


@pytest.mark.parametrize(
    "done",
    [local(2027, 8, 8, 19), local(2027, 8, 13, 8), local(2027, 8, 16, 20)],
    ids=["early", "on the day", "late"],
)
def test_ticking_it_schedules_next_year_from_the_date_not_the_tick(done):
    job = birthday()
    job.done_at = done
    assert settle([job], done) == {"james"}
    assert job.due == local(2028, 8, 6) and job.done_at is None


def test_a_whole_missed_year_skips_to_the_next_notice_not_yet_begun():
    job = birthday()
    job.done_at = local(2028, 8, 9)  # next year's notice had already begun
    settle([job], job.done_at)
    assert job.due == local(2029, 8, 6)


def test_its_notice_starts_at_midnight_across_the_clock_change():
    mam = birthday(on="FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=4", due=local(2026, 3, 28))
    mam.done_at = local(2026, 4, 4, 12)
    settle([mam], mam.done_at)
    assert mam.due == local(2027, 3, 28)
    assert mam.due.astimezone(LONDON).hour == 0


def test_it_describes_itself():
    assert describe(birthday(), []) == "every year on 13 August, shown 7 days before"
    weekly = birthday(on="FREQ=WEEKLY;BYDAY=TU")
    assert describe(weekly, []) == "on fixed dates (FREQ=WEEKLY;BYDAY=TU), shown 7 days before"


def test_it_is_read_from_the_vtodo():
    from icalendar import Todo

    vt = Todo()
    for k, v in [
        ("uid", "chore-bday-james@homelab"),
        ("summary", "Card/present: James W (13 Aug)"),
        ("status", "NEEDS-ACTION"),
        ("x-chore-rank", "20"),
        ("x-chore-every", "P7D"),
        ("x-chore-on", JAMES),
        ("x-chore-due", "20270805T230000Z"),
    ]:
        vt.add(k, v)

    class Fake:
        icalendar_component = vt

    job = CalDAVStore._job_from(Fake(), datetime(2026, 11, 27, tzinfo=UTC))
    assert job.on == JAMES and job.rank == 20 and job.every == 7 * DAY
    assert job.due == local(2027, 8, 6)


# ------------------------------------------------------------------ through the API


@pytest.fixture
def world():
    clock = {"now": local(2027, 8, 1)}
    store = MemoryStore(tasks={"chores": []}, clock=lambda: clock["now"])
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app), store, clock
    app.dependency_overrides.clear()


def titles(client):
    return [t["title"] for t in client.get("/tasks/chores").json()["tasks"]]


def test_a_birthday_among_chores(world):
    client, store, clock = world
    store.add_job("chores", "Dishes", DAY, rank=1)
    store.add_job("chores", "Bed sheets", 7 * DAY, rank=19)
    card = store.add_job("chores", "Card/present: James W (13 Aug)", 7 * DAY, rank=20, on=JAMES)
    assert titles(client) == ["Dishes", "Bed sheets"]  # hidden until the week before

    clock["now"] = local(2027, 8, 6, 9)
    assert titles(client)[-1].startswith("Card/present")
    listed = client.get("/tasks/chores").json()["tasks"]
    assert listed[-1]["repeats"] == "every year on 13 August, shown 7 days before"

    clock["now"] = local(2027, 8, 13, 9)
    for j in store.jobs.values():  # the chores kept up with, as they are day to day
        if not j.on:
            j.due = clock["now"]
    assert titles(client)[0].startswith("Card/present")

    client.post("/tasks/complete", json={"list": "chores", "uid": card.uid})
    assert not any(t.startswith("Card/present") for t in titles(client))
    assert store.jobs[card.uid].due == local(2028, 8, 6)
