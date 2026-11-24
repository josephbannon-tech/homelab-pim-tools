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


def test_one_day_of_a_series_roundtrip(store):
    """Against a real server: moving one day writes a RECURRENCE-ID override,
    moving it again edits that override, cancelling writes an EXDATE, and the
    RRULE is never touched."""
    from zoneinfo import ZoneInfo

    from icalendar import Calendar
    from icalendar import Event as VEvent

    name = store._event_calendar_name
    cal = store._calendar(name)
    tz = ZoneInfo("Europe/London")
    uid = f"pim-tools-live-{uuid.uuid4().hex[:8]}"
    first = (datetime.now(tz) + timedelta(days=40)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    c = Calendar()
    c.add("prodid", "-//pim-tools//test//EN")
    c.add("version", "2.0")
    ve = VEvent()
    ve.add("uid", uid)
    ve.add("summary", "Live nap")
    ve.add("dtstamp", datetime.now(UTC))
    ve.add("dtstart", first)
    ve.add("dtend", first + timedelta(hours=1))
    ve.add("rrule", {"FREQ": "DAILY", "COUNT": 4})
    c.add_component(ve)
    obj = cal.save_event(ical=c.to_ical().decode())

    def days():
        found = store.list_events(first - timedelta(hours=1), first + timedelta(days=4), name)
        return [
            (e.start.astimezone(tz) - first, e.end - e.start, e.title, e.recurring)
            for e in found
            if e.uid == uid
        ]

    hour, day = timedelta(hours=1), timedelta(days=1)
    try:
        assert days() == [(n * day, hour, "Live nap", True) for n in range(4)]

        e, changed = store.change_event(
            name, uid, first + day, first + day + timedelta(minutes=30), None, None, None
        )
        assert changed and e.recurring and e.end - e.start == hour
        assert [d[0] for d in days()] == [0 * day, day + timedelta(minutes=30), 2 * day, 3 * day]

        # again, found by where it now is: the same override is edited, not a second one added
        moved = first + day + timedelta(minutes=30)
        e, changed = store.change_event(
            name, uid, moved, None, moved + timedelta(minutes=40), "Short nap", "today only"
        )
        assert changed and e.title == "Short nap" and e.notes == "today only"
        obj.load()
        parts = obj.icalendar_instance.walk("VEVENT")
        assert len(parts) == 2 and str(parts[0]["RRULE"].to_ical().decode()) == "FREQ=DAILY;COUNT=4"
        assert store.change_event(name, uid, moved, None, None, "Short nap", None)[1] is False

        gone = store.cancel_event(name, uid, first + 2 * day)  # an untouched day
        assert gone.start == first + 2 * day and gone.title == "Live nap"
        assert store.cancel_event(name, uid, moved).title == "Short nap"  # the moved day
        assert [d[0] for d in days()] == [0 * day, 3 * day]
        obj.load()
        assert len(obj.icalendar_instance.walk("VEVENT")) == 1

        with pytest.raises(Exception, match=uid):
            store.cancel_event(name, uid, first + 2 * day)
    finally:
        obj.delete()

    # a single event moves and goes whole
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=41)
    one, _ = store.add_event(f"pim-tools live one-off {uid[-8:]}", start, None, None)
    e, changed = store.change_event(name, one.uid, start, start + hour, None, None, None)
    assert changed and not e.recurring and e.start == start + hour
    assert store.cancel_event(name, one.uid, start + hour).uid == one.uid
    assert all(x.uid != one.uid for x in store.list_events(start - hour, start + 3 * hour))
