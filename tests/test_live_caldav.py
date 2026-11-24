"""End-to-end against a real Radicale. Skipped unless RADICALE_URL is set.
Uses a throwaway list name passed in RADICALE_TEST_LIST (default
'pim-tools-test', which must exist) and cleans up what it creates."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif("RADICALE_URL" not in os.environ, reason="no live Radicale")


@pytest.fixture(scope="module")
def store():
    from pim_tools.store import CalDAVStore

    return CalDAVStore(
        url=os.environ["RADICALE_URL"],
        username=os.environ["RADICALE_USER"],
        password=os.environ["RADICALE_PASSWORD"],
        calendar=os.environ.get("RADICALE_CALENDAR", "personal"),
    )


def test_task_roundtrip(store):
    lst = os.environ.get("RADICALE_TEST_LIST", "pim-tools-test")
    title = f"pim-tools live test {uuid.uuid4().hex[:6]}"
    t, created = store.add_task(lst, title, None)
    assert created and t.uid
    again, created2 = store.add_task(lst, title.upper(), None)
    assert not created2 and again.uid == t.uid
    assert any(x.uid == t.uid for x in store.list_tasks(lst))
    done = store.complete_task(lst, t.uid)
    assert done.completed
    assert all(x.uid != t.uid for x in store.list_tasks(lst))


def test_event_roundtrip(store):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    title = f"pim-tools live event {uuid.uuid4().hex[:6]}"
    e, created = store.add_event(title, start, None, "created by the live test")
    assert created and e.end == start + timedelta(hours=1)
    again, created2 = store.add_event(title, start, None, None)
    assert not created2 and again.uid == e.uid
    found = store.list_events(start - timedelta(hours=1), start + timedelta(hours=2))
    assert any(x.uid == e.uid for x in found)
    # cleanup: delete the object directly over CalDAV
    cal = store._calendar(store._event_calendar_name)
    for obj in cal.search(
        start=start - timedelta(hours=1), end=start + timedelta(hours=2), event=True
    ):
        if str(obj.icalendar_component.get("uid")) == e.uid:
            obj.delete()


def test_repeating_job_roundtrip(store):
    """A job ticked the way a phone does it (STATUS:COMPLETED written straight
    to the server) is rescheduled by the sweep, hidden until due, and its
    chained job comes due."""
    from icalendar import Calendar, Todo

    lst = os.environ.get("RADICALE_TEST_LIST", "pim-tools-test")
    cal = store._calendar(lst)
    tag = uuid.uuid4().hex[:6]
    now = datetime.now(UTC).replace(microsecond=0)

    def put(slug, rank, every, **extra):
        c = Calendar()
        c.add("prodid", "-//pim-tools//test//EN")
        c.add("version", "2.0")
        t = Todo()
        t.add("uid", f"{slug}-{tag}")
        t.add("summary", f"{slug} {tag}")
        t.add("dtstamp", now)
        t.add("status", "NEEDS-ACTION")
        t.add("x-chore-rank", str(rank))
        t.add("x-chore-every", every)
        for k, v in extra.items():
            t.add(k.replace("_", "-"), v)
        c.add_component(t)
        return cal.save_todo(ical=c.to_ical().decode())

    wash = put("wash", 2, "P4D", x_chore_due=now.strftime("%Y%m%dT%H%M%SZ"))
    dry = put("dry", 1, "PT1H", x_chore_after=f"wash-{tag}", x_chore_due="20990101T000000Z")
    try:
        assert [t.title for t in store.list_tasks(lst) if tag in t.title] == [f"wash {tag}"]
        # tick it as a phone would, behind the service's back
        wash.load()  # the listing above gave it a display position
        wash.icalendar_component["status"] = "COMPLETED"
        wash.icalendar_component.add("completed", now - timedelta(hours=2))
        wash.save()
        assert store.sweep(lst) >= 2
        due = [t for t in store.list_tasks(lst) if tag in t.title]
        assert [t.title for t in due] == [f"dry {tag}"]
        assert due[0].repeats == f"1 hour after 'wash {tag}'"
        wash.load()
        vt = wash.icalendar_component
        assert str(vt["status"]) == "NEEDS-ACTION" and "completed" not in vt
        assert vt["dtstart"].dt == now - timedelta(hours=2) + timedelta(days=4)
        done = store.complete_task(lst, f"dry-{tag}")
        assert done.completed and all(tag not in t.title for t in store.list_tasks(lst))
    finally:
        wash.delete()
        dry.delete()
