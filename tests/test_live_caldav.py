"""End-to-end against a real Radicale. Skipped unless RADICALE_URL is set.
Uses a throwaway list name passed in RADICALE_TEST_LIST (default 'chores')
and cleans up what it creates."""

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
    lst = os.environ.get("RADICALE_TEST_LIST", "chores")
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
