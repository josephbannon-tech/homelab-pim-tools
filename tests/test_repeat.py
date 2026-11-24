"""Repeating jobs: the interval runs from when the job was last done, late
jobs climb, and the two links between jobs (after, resets) hold."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from pim_tools.app import app, get_store
from pim_tools.repeat import WAITING, Job, effective_rank, in_order, settle
from pim_tools.store import MemoryStore

T0 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def job(uid, rank, days, due=T0, **kw):
    return Job(uid=uid, title=uid, rank=rank, every=days * DAY, due=due, **kw)


def test_a_job_just_due_keeps_its_rank():
    assert effective_rank(job("hoover", 20, 7), T0) == 20


def test_a_late_job_climbs_in_proportion_to_its_own_interval():
    hoover = job("hoover", 20, 7)
    assert effective_rank(hoover, T0 + 3.5 * DAY) == 10
    assert effective_rank(hoover, T0 + 7 * DAY) == 0
    assert effective_rank(hoover, T0 + 30 * DAY) == 0  # the top is the top
    oven = job("oven", 40, 90)
    assert effective_rank(oven, T0 + 45 * DAY) == 20


def test_due_jobs_come_in_rank_order_and_hidden_ones_are_left_out():
    jobs = [job("oven", 40, 90), job("bins", 2, 7), job("later", 1, 7, due=T0 + DAY)]
    assert [j.uid for j in in_order(jobs, T0)] == ["bins", "oven"]


def test_a_skipped_job_overtakes_a_higher_ranked_one():
    oven = job("oven", 40, 90, due=T0 - 90 * DAY)
    bins = job("bins", 2, 7)
    assert [j.uid for j in in_order([bins, oven], T0)] == ["oven", "bins"]


def test_ticking_a_job_schedules_it_one_interval_after_it_was_done():
    hoover = job("hoover", 20, 7, due=T0 - 3 * DAY)
    hoover.done_at = T0
    assert settle([hoover], T0) == {"hoover"}
    assert hoover.due == T0 + 7 * DAY and hoover.done_at is None


def test_a_chain_appears_one_step_at_a_time():
    wash = job("wash", 7, 4)
    dry = Job("dry", "dry", 2, timedelta(hours=1), WAITING, after="wash")
    away = job("away", 8, 1, due=WAITING, after="dry")
    jobs = [wash, dry, away]
    assert [j.uid for j in in_order(jobs, T0)] == ["wash"]

    wash.done_at = T0
    settle(jobs, T0)
    assert in_order(jobs, T0) == []  # the wash is running
    later = T0 + timedelta(hours=1)
    assert [j.uid for j in in_order(jobs, later)] == ["dry"]

    dry.done_at = later
    settle(jobs, later)
    assert dry.due == WAITING and away.due == later + DAY
    assert wash.due == T0 + 4 * DAY


def test_a_deep_clean_restarts_the_weekly_one():
    weekly = job("hoover", 20, 7, due=T0 - DAY)
    deep = job("deep", 40, 90, resets="hoover")
    deep.done_at = T0
    settle([weekly, deep], T0)
    assert weekly.due == T0 + 7 * DAY and deep.due == T0 + 90 * DAY


def test_positions_are_written_once_then_left_alone():
    jobs = [job("a", 1, 7), job("b", 2, 7), job("c", 3, 7)]
    assert settle(jobs, T0) == {"a", "b", "c"}
    assert [j.position for j in jobs] == [1, 2, 3]
    assert settle(jobs, T0 + timedelta(hours=1)) == set()
    jobs[0].done_at = T0 + timedelta(hours=2)  # ticking one moves nobody else
    assert settle(jobs, T0 + timedelta(hours=2)) == {"a"}
    # c, skipped for a whole interval, overtakes b: only then is the order rewritten
    jobs[1].due = T0 + 6 * DAY
    assert "c" in settle(jobs, T0 + 7 * DAY - timedelta(minutes=1))
    assert jobs[2].position < jobs[1].position


# ------------------------------------------------------------------ through the API


@pytest.fixture
def world():
    clock = {"now": T0}
    store = MemoryStore(tasks={"chores": [], "shopping": []}, clock=lambda: clock["now"])
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app), store, clock
    app.dependency_overrides.clear()


def test_list_shows_due_jobs_in_order_and_completing_one_hides_it(world):
    client, store, clock = world
    store.add_job("chores", "Clean oven", 90 * DAY, rank=40)
    bins = store.add_job("chores", "Bins out", 7 * DAY, rank=2)
    store.add_job("chores", "Not yet", 7 * DAY, rank=1, due=T0 + DAY)

    listed = client.get("/tasks/chores").json()
    assert [t["title"] for t in listed["tasks"]] == ["Bins out", "Clean oven"]
    assert listed["tasks"][0]["repeats"] == "every 7 days"

    done = client.post("/tasks/complete", json={"list": "chores", "uid": bins.uid}).json()
    assert done["completed"] is True
    assert [t["title"] for t in client.get("/tasks/chores").json()["tasks"]] == ["Clean oven"]

    clock["now"] = T0 + 7 * DAY
    again = [t["title"] for t in client.get("/tasks/chores").json()["tasks"]]
    assert again == ["Not yet", "Bins out", "Clean oven"]


def test_jobs_not_due_read_as_completed(world):
    client, store, _ = world
    store.add_job("chores", "Hoover", 7 * DAY, rank=5, due=T0 + 3 * DAY)
    assert client.get("/tasks/chores").json()["tasks"] == []
    both = client.get("/tasks/chores?include_completed=true").json()
    assert both["open"] == 0 and both["tasks"][0]["completed"] is True


def test_ticking_a_job_twice_does_not_push_it_out_twice(world):
    client, store, clock = world
    hoover = store.add_job("chores", "Hoover", 7 * DAY, rank=5)
    body = {"list": "chores", "uid": hoover.uid}
    client.post("/tasks/complete", json=body)
    clock["now"] = T0 + DAY
    client.post("/tasks/complete", json=body)
    assert store.jobs[hoover.uid].due == T0 + 7 * DAY


def test_plain_tasks_on_the_same_list_are_untouched(world):
    client, store, _ = world
    store.add_job("chores", "Hoover", 7 * DAY, rank=5)
    client.post("/tasks", json={"list": "chores", "title": "Buy bin bags"})
    titles = [t["title"] for t in client.get("/tasks/chores").json()["tasks"]]
    assert titles == ["Hoover", "Buy bin bags"]
